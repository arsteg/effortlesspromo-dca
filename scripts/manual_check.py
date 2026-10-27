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


@register(
    "DCA-015", "end-user API auth: valid vs forged/expired/wrong-aud bearer tokens; fake/prod"
)
def _dca_015(ctx: CheckContext) -> dict:
    """Call the protected diagnostic route ``GET /v1/whoami`` with a valid token and with each
    rejected variant (forged signature, expired, wrong audience, 'alg: none', missing header), show
    the actual HTTP responses side by side, then prove the dev fake works while production refuses it.

    Read-only: ``/v1/whoami`` touches no database and changes no state, so every denial is reproduced
    with no side effect.
    """
    from datetime import UTC, datetime, timedelta

    import jwt
    from fastapi.testclient import TestClient

    from app.identity.tokens import build_token_verifier
    from app.main import create_app

    issuer = "https://issuer.dca.local"
    audience = "dca-api"
    secret = "manual-check-signing-secret"  # synthetic, local-only
    subject = "00000000-0000-4000-9000-0000000a11ce"

    def mint(*, sec=secret, iss=issuer, aud=audience, exp_min=5, alg="HS256", key_override=None):
        now = datetime.now(UTC)
        claims = {
            "sub": subject,
            "iss": iss,
            "aud": aud,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=exp_min)).timestamp()),
        }
        return jwt.encode(claims, key_override if key_override is not None else sec, algorithm=alg)

    # A LIVE-configured app (real JWT verifier active) over this run's synthetic, AI-disabled settings.
    configured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token="manual-check-local",
        auth_token_issuer=issuer,
        auth_token_audience=audience,
        auth_token_signing_secret=secret,
        synthetic_sources_enabled=True,
    )
    app = create_app(configured)
    client = TestClient(app, raise_server_exceptions=False)

    def bearer(tok: str) -> dict:
        return {"Authorization": f"Bearer {tok}"}

    try:
        cases = {
            "valid": client.get("/v1/whoami", headers=bearer(mint())),
            "forged_signature": client.get(
                "/v1/whoami", headers=bearer(mint(key_override="attacker-secret"))
            ),
            "expired": client.get("/v1/whoami", headers=bearer(mint(exp_min=-5))),
            "wrong_audience": client.get("/v1/whoami", headers=bearer(mint(aud="other-api"))),
            "alg_none": client.get("/v1/whoami", headers=bearer(mint(alg="none", key_override=""))),
            "missing_header": client.get("/v1/whoami"),
        }
    finally:
        app.state.engine.dispose()

    print("\nGET /v1/whoami with each token (real HS256 verifier active):")
    out: dict = {}
    for name, resp in cases.items():
        body = resp.json()
        print(f"  {name:18} HTTP {resp.status_code}  body={body}")
        out[name] = {"status": resp.status_code, "body": body}

    # Fake verifier: works in development, refused in production (contract disabled, fail-closed).
    dev_unconfigured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
    )
    prod_unconfigured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        deployment_mode=DeploymentMode.LIVE,
        database_url="postgresql+psycopg://user@db/dca",
        service_auth_token="prod-token",
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    dev_verifier = type(build_token_verifier(dev_unconfigured)).__name__
    prod_verifier = build_token_verifier(prod_unconfigured)
    print(f"\nunconfigured development verifier -> {dev_verifier}")
    print(
        "unconfigured production verifier  -> "
        f"{prod_verifier!r} (None = disabled, every call fails closed)"
    )

    out["fake_dev_verifier"] = dev_verifier
    out["production_verifier_disabled"] = prod_verifier is None
    print(
        "\n(the diagnostic route is read-only: no rows created, no state changed by any call above)"
    )
    return out


@register(
    "DCA-016",
    "immutable tenant context: A-only user selects A (ok), B (denied), none (fails closed)",
)
def _dca_016(ctx: CheckContext) -> dict:
    """Use Alice's identity (active in A, expired in B) against ``GET /v1/tenant-context`` with the A
    header, the B header, and no business header, and show the actual HTTP responses side by side.
    Then prove production (identity contract disabled) fails closed with a valid token.

    Read-only: the route touches no database and changes no state, so each grant/denial is
    reproduced with no side effect. ``X-Business-Id`` alone never authorizes — membership is checked.
    """
    from datetime import UTC, datetime, timedelta

    import jwt
    from fastapi.testclient import TestClient

    from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE
    from app.main import create_app

    issuer = "https://issuer.dca.local"
    audience = "dca-api"
    secret = "manual-check-signing-secret"  # synthetic, local-only
    unknown_business = "00000000-0000-4000-8000-00000000ffff"

    def mint(subject: str) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": subject,
                "iss": issuer,
                "aud": audience,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "roles": ["attacker-claimed-superadmin"],  # must be ignored for authorization
            },
            secret,
            algorithm="HS256",
        )

    configured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token="manual-check-local",
        auth_token_issuer=issuer,
        auth_token_audience=audience,
        auth_token_signing_secret=secret,
        synthetic_sources_enabled=True,
    )
    app = create_app(configured)
    client = TestClient(app, raise_server_exceptions=False)
    alice = mint(USER_ALICE)

    def headers(business_id: str | None) -> dict:
        h = {"Authorization": f"Bearer {alice}"}
        if business_id is not None:
            h["X-Business-Id"] = business_id
        return h

    try:
        cases = {
            "alice -> A (member, active)": client.get(
                "/v1/tenant-context", headers=headers(BUSINESS_A)
            ),
            "alice -> B (member, expired)": client.get(
                "/v1/tenant-context", headers=headers(BUSINESS_B)
            ),
            "alice -> unknown business": client.get(
                "/v1/tenant-context", headers=headers(unknown_business)
            ),
            "alice -> no business header": client.get("/v1/tenant-context", headers=headers(None)),
            "no bearer token": client.get(
                "/v1/tenant-context", headers={"X-Business-Id": BUSINESS_A}
            ),
        }
    finally:
        app.state.engine.dispose()

    print("\nGET /v1/tenant-context (membership verified before scope is resolved):")
    out: dict = {}
    for name, resp in cases.items():
        body = resp.json()
        print(f"  {name:32} HTTP {resp.status_code}  body={body}")
        out[name] = {"status": resp.status_code, "body": body}

    # Production: identity contract disabled => tenant resolution fails closed even with a good token.
    prod = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        deployment_mode=DeploymentMode.LIVE,
        database_url="postgresql+psycopg://user@db/dca",
        service_auth_token="prod-token",
        auth_token_issuer=issuer,
        auth_token_audience=audience,
        auth_token_signing_secret=secret,
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    prod_app = create_app(prod)
    prod_disabled = prod_app.state.identity_client is None
    prod_client = TestClient(prod_app, raise_server_exceptions=False)
    try:
        prod_resp = prod_client.get(
            "/v1/tenant-context",
            headers={"Authorization": f"Bearer {mint(USER_ALICE)}", "X-Business-Id": BUSINESS_A},
        )
    finally:
        prod_app.state.engine.dispose()
    print(
        f"\nproduction identity_client disabled -> {prod_disabled}; "
        f"valid-token request -> HTTP {prod_resp.status_code} (fails closed)"
    )
    out["production_contract_disabled"] = prod_disabled
    out["production_valid_token_status"] = prod_resp.status_code
    print(
        "\n(the diagnostic route is read-only: no rows created, no state changed by any call above)"
    )
    return out


@register(
    "DCA-017",
    "tenant ownership: cross-business read returns none, cross-business write is rejected",
)
def _dca_017(ctx: CheckContext) -> dict:
    """Create one synthetic row per business through the scoped repository, then try to read and
    write across the boundary by known id. Show the actual rows and prove the foreign row is
    untouched. Writes are bound to the verified tenant context; a mismatched owner is rejected.
    """
    from app.db.models import DiscoveryRun
    from app.db.tenancy import ScopedRepository, TenantOwnershipError
    from app.identity.fake import BUSINESS_A, BUSINESS_B
    from app.identity.tenant_context import TenantContext

    def ctx_for(business_id: str) -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=business_id,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    def repo(session, business_id: str) -> ScopedRepository:
        return ScopedRepository(session, ctx_for(business_id), DiscoveryRun)

    # One row owned by A, one owned by B.
    with ctx.session_factory() as session:
        row_a = repo(session, BUSINESS_A).create(status="running")
        row_b = repo(session, BUSINESS_B).create(status="running")
        session.commit()
        a_id, b_id = row_a.id, row_b.id

    print("\nseeded one discovery_run per business:")
    print(f"  A row id={a_id}  business_id={BUSINESS_A}")
    print(f"  B row id={b_id}  business_id={BUSINESS_B}")

    out: dict = {"a_id": a_id, "b_id": b_id}

    # Cross-business READ by known id through A's repo.
    with ctx.session_factory() as session:
        repo_a = repo(session, BUSINESS_A)
        own = repo_a.get(a_id)
        foreign = repo_a.get(b_id)
        listed = [r.business_id for r in repo_a.list()]
    print("\ncross-business read through A's scoped repo:")
    print(f"  repo_A.get(A_id)  -> {'row' if own else 'None'}")
    print(f"  repo_A.get(B_id)  -> {'row' if foreign else 'None'}  (foreign id reads as not-found)")
    print(f"  repo_A.list()     -> businesses={listed}  (only A)")
    out["read_own"] = own is not None
    out["read_foreign"] = foreign is not None
    out["list_a_businesses"] = listed

    # Cross-business WRITE: A's repo asked to create a row owned by B.
    rejected = False
    with ctx.session_factory() as session:
        try:
            repo(session, BUSINESS_A).create(business_id=BUSINESS_B, status="running")
        except TenantOwnershipError as exc:
            rejected = True
            session.rollback()
            print(f"\ncross-business write repo_A.create(business_id=B) -> rejected: {exc}")
    out["cross_write_rejected"] = rejected

    # Prove B's row is unchanged and no stray row leaked in.
    with ctx.session_factory() as session:
        b_rows = repo(session, BUSINESS_B).list()
        a_rows = repo(session, BUSINESS_A).list()
    print(
        f"\nafter the rejected write: A has {len(a_rows)} row(s), B has {len(b_rows)} row(s) "
        "(unchanged — the rejection staged nothing)"
    )
    out["a_row_count"] = len(a_rows)
    out["b_row_count"] = len(b_rows)
    return out


@register(
    "DCA-018",
    "postgres RLS: app role sees only its tenant, pooled conn drops prior scope, catalog separate",
)
def _dca_018(ctx: CheckContext) -> dict:
    """Prove row-level security on a disposable PostgreSQL: through a non-superuser, NON-BYPASS app
    role, a scoped transaction sees only its tenant, a reused connection with no scope reads nothing,
    a write for a foreign owner is rejected, and the platform catalog is not reachable by the app
    role. SQLite cannot enforce RLS, so this scenario requires a Postgres ``--database-url``.
    """
    db_url = ctx.settings.database_url or ""
    if not db_url.startswith("postgresql"):
        print(
            "\nDCA-018 requires a disposable PostgreSQL (RLS is not available on SQLite). Run e.g.:"
        )
        print(
            "  docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca "
            "-p 55438:5432 postgres:16-alpine"
        )
        print(
            "  python scripts/manual_check.py DCA-018 "
            "--database-url postgresql+psycopg://postgres:dca@localhost:55438/dca"
        )
        return {"ran": False, "requires_postgres": True}

    import os

    import psycopg
    from alembic import command
    from alembic.config import Config
    from psycopg.errors import InsufficientPrivilege
    from sqlalchemy.engine import make_url

    from app.core.config import get_settings
    from app.db.rls import APP_ROLE, RLS_GUC, tenant_owned_tables
    from app.identity.fake import BUSINESS_A, BUSINESS_B

    schema = "dca_manual_018"
    login = "dca_manual_018_login"
    pw = "manual018pw"  # disposable local-only test role password
    u = make_url(db_url)
    root = Path(__file__).resolve().parent.parent
    su_dsn = f"postgresql://{u.username}:{u.password}@{u.host}:{u.port}/{u.database}"
    app_dsn = f"postgresql://{login}:{pw}@{u.host}:{u.port}/{u.database}"
    seed = (
        "INSERT INTO discovery_runs "
        "(id, business_id, status, source_keys, found, screened_out, suggested, started_at) "
        "VALUES (gen_random_uuid()::text, %s, %s, '[]', 0, 0, 0, now())"
    )

    # --- setup: fresh schema at head, seeded A/B, a non-bypass login member of the app role ---
    su = psycopg.connect(su_dsn, autocommit=True)
    su.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    su.close()
    os.environ["EFFORTLESSPROMO_DCA_ENVIRONMENT"] = "testing"
    os.environ["EFFORTLESSPROMO_DCA_DATABASE_URL"] = db_url
    os.environ["EFFORTLESSPROMO_DCA_DB_SCHEMA"] = schema
    get_settings.cache_clear()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")
    print(f"\nmigrated schema '{schema}' to head; RLS-protected tables: {tenant_owned_tables()}")

    su = psycopg.connect(su_dsn, autocommit=True)
    su.execute(f'SET search_path TO "{schema}"')
    for biz, status in [
        (BUSINESS_A, "running"),
        (BUSINESS_A, "completed"),
        (BUSINESS_B, "running"),
    ]:
        su.execute(seed, (biz, status))
    su.execute(f"DROP ROLE IF EXISTS {login}")
    su.execute(
        f"CREATE ROLE {login} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS IN ROLE {APP_ROLE}"
    )
    su.execute(f'ALTER ROLE {login} SET search_path TO "{schema}"')
    su.close()
    print(f"seeded A=2 rows, B=1 row (as superuser); app login role '{login}' is NOBYPASSRLS")

    out: dict = {"ran": True}
    try:
        conn = psycopg.connect(app_dsn)

        def counts() -> dict:
            rows = conn.execute(
                "SELECT business_id, count(*) FROM discovery_runs GROUP BY business_id "
                "ORDER BY business_id"
            ).fetchall()
            return {biz: n for biz, n in rows}

        print("\nqueries through the application role:")
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, BUSINESS_A))
        out["scope_a"] = counts()
        print(f"  scope=A              -> {out['scope_a']}")
        conn.commit()  # ends tx; the tx-local scope is discarded

        out["no_scope_reused"] = counts()
        print(f"  no scope (same conn) -> {out['no_scope_reused']}  (prior scope did not leak)")
        conn.commit()

        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, BUSINESS_B))
        out["scope_b"] = counts()
        print(f"  scope=B              -> {out['scope_b']}")
        conn.commit()

        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, BUSINESS_A))
        try:
            conn.execute(seed, (BUSINESS_B, "running"))
            out["foreign_write"] = "UNEXPECTEDLY ALLOWED"
        except InsufficientPrivilege:
            out["foreign_write"] = "rejected"
        conn.rollback()
        print(f"  insert owner=B under scope=A -> {out['foreign_write']}")

        try:
            conn.execute("SELECT count(*) FROM tenant_agents").fetchone()
            out["catalog_access"] = "UNEXPECTEDLY READABLE"
        except InsufficientPrivilege:
            out["catalog_access"] = "no access (separate path)"
        conn.rollback()
        print(f"  app role -> tenant_agents catalog -> {out['catalog_access']}")
        conn.close()
    finally:
        su = psycopg.connect(su_dsn, autocommit=True)
        su.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        su.execute(f"DROP ROLE IF EXISTS {login}")
        su.close()
        get_settings.cache_clear()
        print(f"\ncleaned up schema '{schema}' and role '{login}'")
    return out


@register(
    "DCA-019",
    "operation permissions: admin vs ordinary user vs platform operator across two scope planes",
)
def _dca_019(ctx: CheckContext) -> dict:
    """Call the two restricted probe routes with each fixture role and show the authorization
    outcomes side by side: a business admin approves sources; an ordinary user cannot; a platform
    operator (no membership) is denied the client-business action yet is allowed its own platform
    scope. Read-only probes — no source is approved, no state changes.
    """
    from datetime import UTC, datetime, timedelta

    import jwt
    from fastapi.testclient import TestClient

    from app.identity.fake import BUSINESS_A, USER_ALICE, USER_BOB, USER_CAROL
    from app.main import create_app

    issuer = "https://issuer.dca.local"
    audience = "dca-api"
    secret = "manual-check-signing-secret"  # synthetic, local-only
    operator = "00000000-0000-4000-9000-00000090e701"

    def mint(subject: str, roles: list[str] | None = None) -> str:
        now = datetime.now(UTC)
        claims: dict = {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        }
        if roles is not None:
            claims["roles"] = roles
        return jwt.encode(claims, secret, algorithm="HS256")

    configured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token="manual-check-local",
        auth_token_issuer=issuer,
        auth_token_audience=audience,
        auth_token_signing_secret=secret,
        synthetic_sources_enabled=True,
    )
    app = create_app(configured)
    client = TestClient(app, raise_server_exceptions=False)

    def hdrs(token: str, business_id: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {token}"}
        if business_id is not None:
            h["X-Business-Id"] = business_id
        return h

    try:
        business_cases = {
            "alice  owner@A   -> approve source in A": client.get(
                "/v1/authz/business/source-approval", headers=hdrs(mint(USER_ALICE), BUSINESS_A)
            ),
            "carol  viewer@A  -> approve source in A": client.get(
                "/v1/authz/business/source-approval", headers=hdrs(mint(USER_CAROL), BUSINESS_A)
            ),
            "bob    admin@B   -> approve source in A": client.get(
                "/v1/authz/business/source-approval", headers=hdrs(mint(USER_BOB), BUSINESS_A)
            ),
            "operator         -> approve source in A": client.get(
                "/v1/authz/business/source-approval",
                headers=hdrs(mint(operator, ["platform_operator"]), BUSINESS_A),
            ),
        }
        platform_cases = {
            "operator         -> platform ops": client.get(
                "/v1/authz/platform/ops", headers=hdrs(mint(operator, ["platform_operator"]))
            ),
            "alice  owner@A   -> platform ops": client.get(
                "/v1/authz/platform/ops", headers=hdrs(mint(USER_ALICE, ["owner"]))
            ),
        }
    finally:
        app.state.engine.dispose()

    out: dict = {"business": {}, "platform": {}}
    print("\nbusiness plane — approve a source (administrator scope):")
    for name, resp in business_cases.items():
        verdict = "ALLOW" if resp.status_code == 200 else f"DENY ({resp.status_code})"
        print(f"  {name:42} -> {verdict}")
        out["business"][name] = resp.status_code
    print("\nplatform plane — read platform ops (operator scope):")
    for name, resp in platform_cases.items():
        verdict = "ALLOW" if resp.status_code == 200 else f"DENY ({resp.status_code})"
        print(f"  {name:42} -> {verdict}")
        out["platform"][name] = resp.status_code

    print(
        "\nnote: a platform operator is DENIED the client-business action (no membership, no bypass) "
        "yet ALLOWED its own platform scope; decisions are audited to the 'dca.authz' logger."
    )
    print("(all probes are read-only: no source approved, no state changed)")
    return out


@register(
    "DCA-020",
    "scoped service credentials: one operation/business only; expired & wrong-scope rejected",
)
def _dca_020(ctx: CheckContext) -> dict:
    """Call a service route with a credential scoped to tenants.read@A and show that it is accepted
    only for that operation and business — another operation, another business, an expired
    credential, and an end-user (public-form) token are all rejected. Read-only probes; no state
    changes.
    """
    from datetime import UTC, datetime, timedelta

    import jwt
    from fastapi.testclient import TestClient

    from app.identity.fake import BUSINESS_A, BUSINESS_B
    from app.main import create_app

    svc_issuer = "https://platform.dca.local"
    svc_audience = "dca-service"
    svc_secret = "manual-check-service-secret"  # synthetic, local-only
    user_audience = "dca-api"
    user_secret = "manual-check-user-secret"
    service = "platform-integration"

    def mint(*, operations, businesses, secret=svc_secret, audience=svc_audience, exp_min=5):
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": service,
                "iss": svc_issuer,
                "aud": audience,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=exp_min)).timestamp()),
                "operations": operations,
                "businesses": businesses,
            },
            secret,
            algorithm="HS256",
        )

    configured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token="manual-check-local",
        service_token_issuer=svc_issuer,
        service_token_audience=svc_audience,
        service_token_signing_secret=svc_secret,
        synthetic_sources_enabled=True,
    )
    app = create_app(configured)
    client = TestClient(app, raise_server_exceptions=False)

    def hdr(token: str) -> dict:
        return {"X-Service-Credential": token}

    scoped = mint(operations=["tenants.read"], businesses=[BUSINESS_A])
    expired = mint(operations=["tenants.read"], businesses=[BUSINESS_A], exp_min=-5)
    public_form = mint(
        operations=["tenants.read"],
        businesses=[BUSINESS_A],
        secret=user_secret,
        audience=user_audience,
    )
    try:
        cases = {
            "tenants.read @A  (in scope)": client.get(
                f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=hdr(scoped)
            ),
            "discovery.trigger @A  (other op)": client.get(
                f"/v1/service/probe/discovery-trigger/{BUSINESS_A}", headers=hdr(scoped)
            ),
            "tenants.read @B  (other business)": client.get(
                f"/v1/service/probe/tenants-read/{BUSINESS_B}", headers=hdr(scoped)
            ),
            "tenants.read @A  (expired cred)": client.get(
                f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=hdr(expired)
            ),
            "tenants.read @A  (public-form token)": client.get(
                f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=hdr(public_form)
            ),
            "tenants.read @A  (no credential)": client.get(
                f"/v1/service/probe/tenants-read/{BUSINESS_A}"
            ),
        }
    finally:
        app.state.engine.dispose()

    print("\nservice credential scoped to operation=tenants.read, business=A:")
    out: dict = {}
    for name, resp in cases.items():
        verdict = "ALLOW" if resp.status_code == 200 else f"DENY ({resp.status_code})"
        print(f"  {name:38} -> {verdict}")
        out[name] = resp.status_code
    print(
        "\nnote: a credential cannot widen its own operation or business; a public-form (end-user) "
        "token is never accepted; decisions are audited to the 'dca.authz' logger."
    )
    print("(all probes are read-only: no state changed)")
    return out


@register(
    "DCA-021",
    "feature gate: off-by-default blocks all; on + per-business narrowing (A on, B off)",
)
def _dca_021(ctx: CheckContext) -> dict:
    """Request the same protected work operation (discovery run) under three states and show the
    outcomes: feature OFF blocks even a fully-enabled business; feature ON runs for the narrowed-in
    business A; and B (registered but not enabled) stays blocked — enabling A never enables B. Each
    denial leaves no run recorded.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    biz_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    biz_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}

    def settings_with(feature: bool):
        return type(ctx.settings)(
            _env_file=None,  # type: ignore[call-arg]
            environment="development",
            database_url=ctx.settings.database_url,
            service_auth_token=ctx.settings.service_auth_token,
            synthetic_sources_enabled=True,
            feature_enabled=feature,
        )

    def register(client, business_id, *, enable):
        client.post("/v1/tenants", json={"businessId": business_id}, headers=auth)
        if enable:
            client.post(f"/v1/tenants/{business_id}/enable", headers=auth)
        client.put(
            f"/v1/tenants/{business_id}/icp",
            json={"industries": [], "regions": [], "roles": []},
            headers=auth,
        )

    out: dict = {}

    # State 1: feature OFF — a fully enabled business still cannot start work.
    app_off = create_app(settings_with(False))
    client_off = TestClient(app_off, raise_server_exceptions=False)
    try:
        register(client_off, biz_a, enable=True)
        r = client_off.post(f"/v1/tenants/{biz_a}/discovery/run", headers=auth)
        runs = client_off.get(f"/v1/tenants/{biz_a}/runs", headers=auth).json()
        print("\nfeature OFF (platform master switch):")
        print(
            f"  A (enabled) -> start work -> HTTP {r.status_code} ({r.json()['error']['message']})"
        )
        print(f"  A runs recorded: {len(runs)} (denial created no state)")
        out["feature_off_A"] = {"status": r.status_code, "runs": len(runs)}
    finally:
        app_off.state.engine.dispose()

    # State 2: feature ON — A narrowed in, B registered but not enabled.
    app_on = create_app(settings_with(True))
    client_on = TestClient(app_on, raise_server_exceptions=False)
    try:
        register(client_on, biz_a, enable=True)
        register(client_on, biz_b, enable=False)
        ra = client_on.post(f"/v1/tenants/{biz_a}/discovery/run", headers=auth)
        rb = client_on.post(f"/v1/tenants/{biz_b}/discovery/run", headers=auth)
        b_runs = client_on.get(f"/v1/tenants/{biz_b}/runs", headers=auth).json()
        print("\nfeature ON (per-business narrowing):")
        print(
            f"  A (enabled)     -> start work -> HTTP {ra.status_code} ({ra.json().get('status', '-')})"
        )
        msg_b = rb.json().get("error", {}).get("message", rb.json().get("status", "-"))
        print(f"  B (not enabled) -> start work -> HTTP {rb.status_code} ({msg_b})")
        print(f"  B runs recorded: {len(b_runs)} (enabling A did not enable B)")
        out["feature_on_A"] = ra.status_code
        out["feature_on_B"] = {"status": rb.status_code, "runs": len(b_runs)}
    finally:
        app_on.state.engine.dispose()

    print(
        "\n(the feature is off by default; nothing above enabled a business that was not toggled)"
    )
    return out


@register(
    "DCA-022",
    "entitlement snapshots: active/expired/missing fixtures + stale refusal; production refuses fake",
)
def _dca_022(ctx: CheckContext) -> dict:
    """Resolve active / expired / missing entitlement fixtures through the synthetic fake and show
    each snapshot's capabilities, then the authorize_paid_work decision for a capability — proving
    absent, expired and stale snapshots cannot authorize paid work. Finally show production refuses
    the fake. All synthetic; no paid action, no state change.
    """
    from datetime import timedelta

    from app.entitlements import (
        EntitlementContractDisabled,
        EntitlementRequest,
        FakeEntitlementClient,
        authorize_paid_work,
        build_entitlement_client,
    )
    from app.identity.fake import BUSINESS_A, BUSINESS_B

    client = build_entitlement_client(ctx.settings)
    now = client.resolve(EntitlementRequest(business_id=BUSINESS_A)).as_of  # the fake's clock

    cases = {
        "A (active synthetic-growth)": BUSINESS_A,
        "B (expired synthetic-starter)": BUSINESS_B,
        "stranger (no subscription)": "stranger-business",
    }
    print("\nentitlement snapshots (all synthetic):")
    out: dict = {}
    for label, business_id in cases.items():
        snap = client.resolve(EntitlementRequest(business_id=business_id))
        decision = authorize_paid_work(snap, "discovery", now=now)
        print(
            f"  {label:32} status={snap.status.value:8} synthetic={snap.synthetic} "
            f"caps={list(snap.capabilities)}"
        )
        print(
            f"      authorize 'discovery' -> {'ALLOW' if decision.allowed else 'DENY'} "
            f"({decision.reason})"
        )
        out[label] = {"status": snap.status.value, "decision": decision.reason}

    # Stale: an active A snapshot fetched 48h ago is refused even though its own expiry is far off.
    stale = FakeEntitlementClient(now=now - timedelta(hours=48)).resolve(
        EntitlementRequest(business_id=BUSINESS_A)
    )
    stale_decision = authorize_paid_work(stale, "discovery", now=now)
    print(
        f"\n  A fetched 48h ago (status={stale.status.value}) authorize 'discovery' -> "
        f"{'ALLOW' if stale_decision.allowed else 'DENY'} ({stale_decision.reason})"
    )
    out["stale_A"] = stale_decision.reason

    # Production refuses the synthetic fake (contract disabled until the live adapter ships).
    prod = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        database_url="postgresql+psycopg://user@db/dca",
        service_auth_token="prod-token",
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    refused = False
    try:
        build_entitlement_client(prod)
    except EntitlementContractDisabled as exc:
        refused = True
        print(f"\nproduction build_entitlement_client -> refused: {exc}")
    out["production_refused"] = refused

    print("\n(all snapshots are synthetic; no paid action was taken and no state changed)")
    return out


@register(
    "DCA-023",
    "entitlement freshness: fresh authorizes; outage uses cache only while fresh; stale/revoked deny",
)
def _dca_023(ctx: CheckContext) -> dict:
    """Authorize with a fresh snapshot, then simulate an entitlement-host outage and advance the
    fixture clock: the cached projection authorizes while fresh, then fails closed once stale — so a
    prior-active subscription cannot keep authorizing paid work indefinitely. Synthetic; the gate
    writes only a bounded projection row, no paid action.
    """
    from datetime import timedelta

    from app.entitlements import (
        EntitlementRequest,
        EntitlementSnapshot,
        FakeEntitlementClient,
    )
    from app.identity.fake import BUSINESS_A
    from app.services.entitlement_gate import EntitlementGateError, ensure_entitled

    settings = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token=ctx.settings.service_auth_token,
        entitlement_enforcement_enabled=True,
    )
    t0 = FakeEntitlementClient().resolve(EntitlementRequest(business_id=BUSINESS_A)).as_of

    class _Outage:
        def resolve(self, request):
            raise RuntimeError("entitlement host unreachable")

    live = FakeEntitlementClient(now=t0)  # active A snapshot captured at t0

    def probe(client, *, at, label):
        with ctx.session_factory() as session:
            try:
                snap: EntitlementSnapshot = ensure_entitled(
                    session, settings, client, BUSINESS_A, "discovery", now=at
                )
                print(f"  {label:44} -> ALLOW (status={snap.status.value})")
                return "allow"
            except EntitlementGateError as exc:
                print(f"  {label:44} -> DENY ({exc.reason})")
                return exc.reason

    out: dict = {}
    print("\nchargeable op gated on entitlement freshness (business A):")
    out["fresh"] = probe(live, at=t0, label="fresh live snapshot at t0")
    out["outage_1h"] = probe(
        _Outage(), at=t0 + timedelta(hours=1), label="host outage, +1h (cache fresh)"
    )
    out["outage_25h"] = probe(
        _Outage(), at=t0 + timedelta(hours=25), label="host outage, +25h (cache stale)"
    )

    # Revocation: a live resolve that now reports no subscription clears the cache and fails closed.
    revoked = FakeEntitlementClient(
        plans={},
        now=t0 + timedelta(hours=2),  # no plan for A => status NONE
    )
    out["revoked"] = probe(
        revoked, at=t0 + timedelta(hours=2), label="live revocation (no subscription)"
    )

    print(
        "\n(the only state written is a bounded entitlement projection row; no paid action occurred)"
    )
    return out


@register(
    "DCA-024",
    "agent config: change A's data mode (B unchanged); auto-accept and bad modes refused",
)
def _dca_024(ctx: CheckContext) -> dict:
    """Register A and B, change A's data mode and show B's config is untouched, then attempt the
    unsupported auto-accept mode and verify it is refused with no state change. Suggestion mode
    never grants send. Read via the real HTTP API; no paid action.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    biz_a = "11111111-1111-1111-1111-111111111111"
    biz_b = "22222222-2222-2222-2222-222222222222"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}

    app = create_app(ctx.settings)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        for biz in (biz_a, biz_b):
            client.post("/v1/tenants", json={"businessId": biz}, headers=auth)

        a_before = client.get(f"/v1/tenants/{biz_a}/config", headers=auth).json()
        b_before = client.get(f"/v1/tenants/{biz_b}/config", headers=auth).json()
        print(f"\ndefaults:  A dataMode={a_before['dataMode']}  B dataMode={b_before['dataMode']}")

        client.put(
            f"/v1/tenants/{biz_a}/config",
            json={"dataMode": "suggest_platform_data", "enabled": True},
            headers=auth,
        )
        a_after = client.get(f"/v1/tenants/{biz_a}/config", headers=auth).json()
        b_after = client.get(f"/v1/tenants/{biz_b}/config", headers=auth).json()
        print(
            f"after A->suggest_platform_data:  A dataMode={a_after['dataMode']} "
            f"enabled={a_after['enabled']}  |  B dataMode={b_after['dataMode']} (unchanged)"
        )
        print(
            f"  A suggestionModeGrantsSend={a_after['suggestionModeGrantsSend']} (no send granted)"
        )
        out["a_mode"] = a_after["dataMode"]
        out["b_mode_unchanged"] = b_after["dataMode"]
        out["grants_send"] = a_after["suggestionModeGrantsSend"]

        auto = client.put(
            f"/v1/tenants/{biz_a}/config",
            json={"dataMode": "my_data_only", "autoAccept": True},
            headers=auth,
        )
        bad = client.put(
            f"/v1/tenants/{biz_a}/config", json={"dataMode": "auto_accept"}, headers=auth
        )
        a_final = client.get(f"/v1/tenants/{biz_a}/config", headers=auth).json()
        print(
            f"\nattempt autoAccept=true   -> HTTP {auto.status_code} "
            f"({auto.json()['error']['message']})"
        )
        print(
            f"attempt dataMode=auto_accept -> HTTP {bad.status_code} "
            f"({bad.json()['error']['message']})"
        )
        print(f"A dataMode after refusals: {a_final['dataMode']} (unchanged by the rejected calls)")
        out["auto_accept_status"] = auto.status_code
        out["bad_mode_status"] = bad.status_code
        out["a_mode_final"] = a_final["dataMode"]
    finally:
        app.state.engine.dispose()

    print("\n(config changes are per-business; suggestion mode grants no send; no paid action)")
    return out


@register(
    "DCA-025",
    "tenant-isolation matrix: read/update/list across A/B, leak detection, unresolved-scope deny",
)
def _dca_025(ctx: CheckContext) -> dict:
    """Run the authorization matrix over the tenant-owned resources for synthetic A/B: show that
    cross-tenant known-ID reads/lists are isolated, that the same matrix FAILS against a leaky
    (filter-removed) repository, that an unresolved scope reads nothing, and inspect one HTTP
    known-ID denial and its safe 404 body. Read-only probes; no paid action.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.db.models import DiscoveryRun
    from app.db.tenancy import ScopedRepository
    from app.identity.fake import BUSINESS_A, BUSINESS_B
    from app.identity.tenant_context import TenantContext
    from app.main import create_app

    def ctx_for(business_id: str) -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=business_id,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    out: dict = {}

    # Repository matrix over discovery_runs (representative tenant-owned resource).
    with ctx.session_factory() as session:
        a = ScopedRepository(session, ctx_for(BUSINESS_A), DiscoveryRun).create(status="running")
        b = ScopedRepository(session, ctx_for(BUSINESS_B), DiscoveryRun).create(status="running")
        session.commit()
        repo_a = ScopedRepository(session, ctx_for(BUSINESS_A), DiscoveryRun)
        read_own = repo_a.get(a.id) is not None
        read_foreign = repo_a.get(b.id) is not None
        list_a = [r.id for r in repo_a.list()]
        unknown = ScopedRepository(session, ctx_for("unresolved-scope-0000"), DiscoveryRun)
        unresolved_list = unknown.list()

    print("\nrepository matrix (discovery_runs), scope=A:")
    print(f"  get(A_id)          -> {'row' if read_own else 'None'}")
    print(
        f"  get(B_id)          -> {'row' if read_foreign else 'None'}  (foreign id reads as none)"
    )
    print(f"  list()             -> {len(list_a)} row(s), only A = {list_a == [a.id]}")
    print(f"  unresolved scope   -> list() = {unresolved_list} (fail-closed)")
    out["read_own"] = read_own
    out["read_foreign"] = read_foreign
    out["list_only_a"] = list_a == [a.id]
    out["unresolved_empty"] = unresolved_list == []

    # Leak detection: the SAME check against a filter-removed repository must surface the leak.
    class _Leaky(ScopedRepository):
        def select(self):
            return select(self._model)

        def get(self, entity_id):
            return self._session.get(self._model, entity_id)

    with ctx.session_factory() as session:
        la = ScopedRepository(session, ctx_for(BUSINESS_A), DiscoveryRun).create(status="running")
        ScopedRepository(session, ctx_for(BUSINESS_B), DiscoveryRun).create(status="running")
        session.commit()
        leaky_a = _Leaky(session, ctx_for(BUSINESS_A), DiscoveryRun)
        leaked = len(leaky_a.list()) > 1  # a leaky repo sees BOTH businesses' rows
    print(
        f"\nleak detection: filter-removed repo sees >1 business's rows -> {leaked} (matrix catches)"
    )
    out["leak_detected"] = leaked
    _ = la  # seeded for clarity

    # One HTTP known-ID denial + its safe 404 body.
    biz_a = "11111111-1111-1111-1111-111111111111"
    biz_b = "22222222-2222-2222-2222-222222222222"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}
    http_settings = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token=ctx.settings.service_auth_token,
        synthetic_sources_enabled=True,
        feature_enabled=True,  # needed so the discovery run produces candidates
    )
    app = create_app(http_settings)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        for biz in (biz_a, biz_b):
            client.post("/v1/tenants", json={"businessId": biz}, headers=auth)
            client.post(f"/v1/tenants/{biz}/enable", headers=auth)
            client.put(
                f"/v1/tenants/{biz}/icp",
                json={"industries": [], "regions": [], "roles": []},
                headers=auth,
            )
        client.post(f"/v1/tenants/{biz_a}/discovery/run", headers=auth)
        cand = client.get(f"/v1/tenants/{biz_a}/candidates", headers=auth).json()[0]
        denial = client.post(f"/v1/tenants/{biz_b}/candidates/{cand['id']}/accept", headers=auth)
    finally:
        app.state.engine.dispose()
    print(
        f"\nHTTP known-ID denial: accept A's candidate under B -> HTTP {denial.status_code} "
        f"body={denial.json()}"
    )
    out["http_denial_status"] = denial.status_code

    print("\n(all checks read-only; the rejected cross-tenant call changed no row)")
    return out


@register(
    "DCA-026",
    "transactional audit: commit one audited mutation, roll back another; refuse raw contact data",
)
def _dca_026(ctx: CheckContext) -> dict:
    """Prove the audit sink is atomic with the caller's transaction and never persists raw
    secret/contact data. For synthetic Business A: seed two candidates, then

      1. COMMIT a unit of work that accepts candidate #1 AND records its audit event -> both persist;
      2. ROLL BACK a unit of work that accepts candidate #2 AND records its audit event (a downstream
         step fails) -> neither the status change nor the audit row survives;
      3. attempt to audit with a RAW CONTACT reference -> refused before anything is staged.

    Shows the actual candidate statuses and audit rows after each step. No AI/paid action.
    """
    from sqlalchemy import select

    from app.audit import ActorType, AuditActor, AuditPayloadError, AuditSink
    from app.db.models import AuditEvent, DiscoveryRun, ProspectCandidate
    from app.identity.fake import BUSINESS_A
    from app.identity.tenant_context import TenantContext

    def ctx_a() -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=BUSINESS_A,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    def seed_candidate(session, identifier: str) -> str:
        run = DiscoveryRun(business_id=BUSINESS_A, status="completed")
        session.add(run)
        session.flush()
        cand = ProspectCandidate(
            business_id=BUSINESS_A,
            run_id=run.id,
            org_name="Synthetic Org",
            org_identifier=identifier,
            source_key="synthetic",
            source_ref="synthetic://1",
            trust_tier="official_registry",
            status="suggested",
        )
        session.add(cand)
        session.flush()
        return cand.id

    out: dict = {}

    with ctx.session_factory() as session:
        c1 = seed_candidate(session, "SYN-1")
        c2 = seed_candidate(session, "SYN-2")
        session.commit()

    # 1) committed audited mutation
    with ctx.session_factory() as session:
        cand = session.get(ProspectCandidate, c1)
        cand.status = "accepted"
        AuditSink.for_context(session, ctx_a()).record(
            action="candidate.accept", reason="ok", references={"candidateId": c1}
        )
        session.commit()

    # 2) rolled-back audited mutation (downstream failure after staging both)
    rolled_back = False
    session = ctx.session_factory()
    try:
        cand = session.get(ProspectCandidate, c2)
        cand.status = "accepted"
        AuditSink.for_context(session, ctx_a()).record(
            action="candidate.accept", reason="ok", references={"candidateId": c2}
        )
        raise RuntimeError("simulated downstream failure")
    except RuntimeError:
        session.rollback()
        rolled_back = True
    finally:
        session.close()

    # 3) refuse a raw-contact reference (service actor this time) — nothing staged
    refused = False
    refusal = ""
    with ctx.session_factory() as session:
        try:
            AuditSink(
                session,
                business_id=BUSINESS_A,
                actor=AuditActor("platform-api", ActorType.SERVICE),
            ).record(action="candidate.accept", references={"ownerEmail": "founder@acme.co.in"})
        except AuditPayloadError as exc:
            refused = True
            refusal = str(exc)
        session.commit()

    # Inspect the actual persisted state.
    with ctx.session_factory() as session:
        s1 = session.get(ProspectCandidate, c1).status
        s2 = session.get(ProspectCandidate, c2).status
        audits = session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)).all()

    print("\ncandidate statuses after the three steps:")
    print(f"  #1 (committed)    status={s1}   (expected: accepted)")
    print(f"  #2 (rolled back)  status={s2}  (expected: suggested — change discarded)")
    print(
        f"\naudit_events rows persisted: {len(audits)} (expected: 1 — only the committed mutation)"
    )
    for a in audits:
        print(
            f"  - actor={a.actor_type}:{a.actor_id}  scope={a.business_id}  "
            f"action={a.action}  reason={a.reason}  refs={a.references}"
        )
    print(f"\nrolled-back unit of work: {rolled_back} (no audit row for #2)")
    print(f"raw-contact audit refused: {refused}  ({refusal if refused else 'NOT REFUSED!'})")

    out["c1_status"] = s1
    out["c2_status"] = s2
    out["audit_row_count"] = len(audits)
    out["rolled_back"] = rolled_back
    out["raw_contact_refused"] = refused
    print("\n(atomic with the caller's transaction; opaque references only; no paid action)")
    return out


@register(
    "DCA-027",
    "transactional outbox: commit one intent, roll back another; dup intent & raw contact refused",
)
def _dca_027(ctx: CheckContext) -> dict:
    """Prove the outbox sink stages durable integration intents atomically with the local change and
    never persists raw secret/contact data — and performs NO remote delivery. For synthetic Business
    A: seed two candidates, then

      1. COMMIT a unit of work that accepts candidate #1 AND stages its outbox intent -> both persist
         (the row is PENDING, with a stable event id derived from the logical intent);
      2. ROLL BACK a unit of work that accepts candidate #2 AND stages its outbox intent (a downstream
         step fails) -> neither the status change NOR the outbox intent survives;
      3. re-stage candidate #1's SAME logical intent in a fresh transaction -> rejected by the
         (business_id, event_id) uniqueness constraint; nothing changes;
      4. attempt to stage an intent carrying a RAW CONTACT payload -> refused before anything staged.

    Shows the actual candidate statuses and outbox rows after each step. No AI/paid action, no HTTP.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.db.models import DiscoveryRun, OutboxMessage, OutboxStatus, ProspectCandidate
    from app.identity.fake import BUSINESS_A
    from app.identity.tenant_context import TenantContext
    from app.outbox import OutboxPayloadError, OutboxSink

    def ctx_a() -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=BUSINESS_A,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    def seed_candidate(session, identifier: str) -> str:
        run = DiscoveryRun(business_id=BUSINESS_A, status="completed")
        session.add(run)
        session.flush()
        cand = ProspectCandidate(
            business_id=BUSINESS_A,
            run_id=run.id,
            org_name="Synthetic Org",
            org_identifier=identifier,
            source_key="synthetic",
            source_ref="synthetic://1",
            trust_tier="official_registry",
            status="suggested",
        )
        session.add(cand)
        session.flush()
        return cand.id

    out: dict = {}

    with ctx.session_factory() as session:
        c1 = seed_candidate(session, "SYN-1")
        c2 = seed_candidate(session, "SYN-2")
        session.commit()

    # 1) committed local change + staged outbox intent
    with ctx.session_factory() as session:
        cand = session.get(ProspectCandidate, c1)
        cand.status = "accepted"
        OutboxSink.for_context(session, ctx_a()).enqueue(
            event_type="candidate.accepted",
            dedup_key=f"{c1}:accept",
            payload={"candidateId": c1},
        )
        session.commit()

    # 2) rolled-back local change + outbox intent (downstream failure after staging both)
    rolled_back = False
    session = ctx.session_factory()
    try:
        cand = session.get(ProspectCandidate, c2)
        cand.status = "accepted"
        OutboxSink.for_context(session, ctx_a()).enqueue(
            event_type="candidate.accepted",
            dedup_key=f"{c2}:accept",
            payload={"candidateId": c2},
        )
        raise RuntimeError("simulated downstream failure")
    except RuntimeError:
        session.rollback()
        rolled_back = True
    finally:
        session.close()

    # 3) duplicate logical intent for candidate #1 is rejected by the uniqueness constraint
    dup_rejected = False
    with ctx.session_factory() as session:
        OutboxSink.for_context(session, ctx_a()).enqueue(
            event_type="candidate.accepted",
            dedup_key=f"{c1}:accept",
            payload={"attempt": 2},
        )
        try:
            session.commit()
        except IntegrityError:
            dup_rejected = True
            session.rollback()

    # 4) refuse a raw-contact payload — nothing staged
    refused = False
    refusal = ""
    with ctx.session_factory() as session:
        try:
            OutboxSink.for_context(session, ctx_a()).enqueue(
                event_type="candidate.accepted",
                dedup_key=f"{c1}:notify",
                payload={"ownerEmail": "founder@acme.co.in"},
            )
        except OutboxPayloadError as exc:
            refused = True
            refusal = str(exc)
        session.commit()

    # Inspect the actual persisted state.
    with ctx.session_factory() as session:
        s1 = session.get(ProspectCandidate, c1).status
        s2 = session.get(ProspectCandidate, c2).status
        rows = session.scalars(select(OutboxMessage).order_by(OutboxMessage.created_at)).all()

    print("\ncandidate statuses after the four steps:")
    print(f"  #1 (committed)    status={s1}   (expected: accepted)")
    print(f"  #2 (rolled back)  status={s2}  (expected: suggested — change discarded)")
    print(
        f"\noutbox_messages rows persisted: {len(rows)} (expected: 1 — only the committed intent)"
    )
    for r in rows:
        print(
            f"  - event_type={r.event_type}  status={r.status}  version={r.version}  "
            f"event_id={r.event_id}  dedup_key={r.dedup_key}  payload={r.payload}"
        )
    print(f"\nrolled-back unit of work: {rolled_back} (no outbox intent for #2)")
    print(f"duplicate logical intent refused: {dup_rejected} (uniqueness constraint)")
    print(f"raw-contact intent refused: {refused}  ({refusal if refused else 'NOT REFUSED!'})")

    out["c1_status"] = s1
    out["c2_status"] = s2
    out["outbox_row_count"] = len(rows)
    out["outbox_statuses"] = [r.status for r in rows]
    out["all_pending"] = all(r.status == OutboxStatus.PENDING.value for r in rows)
    out["rolled_back"] = rolled_back
    out["duplicate_rejected"] = dup_rejected
    out["raw_contact_refused"] = refused
    print(
        "\n(atomic with the local change; opaque payloads only; NO remote delivery, no paid action)"
    )
    return out


@register(
    "DCA-028",
    "inbox dedup: handle once, replay is a no-op, failed processing retries; raw contact refused",
)
def _dca_028(ctx: CheckContext) -> dict:
    """Prove inbound-event deduplication with transactional handler completion. For synthetic
    Business A, seed two candidates and deliver a synthetic event per candidate through the
    idempotent-receiver unit of work (fast-path skip if already seen; else mutate + record the
    receipt and commit both together):

      1. deliver event E1 for candidate #1 -> handled: status changes AND one receipt is recorded;
      2. deliver E1 AGAIN (a replay) -> short-circuited by the fast path, NO second application;
      2b. re-register E1 WITHOUT the fast path -> rejected by the (business_id, source, id) uniqueness
          constraint, so even a careless caller cannot apply the change twice;
      3. deliver event E2 for candidate #2 but FAIL mid-handler -> rolled back: neither the status
         change NOR the receipt survives, so the retry (4) applies it cleanly exactly once;
      5. attempt to record a receipt carrying a RAW CONTACT payload -> refused before anything staged.

    Shows the actual candidate statuses and inbox rows after each step. No AI/paid action, no HTTP.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.db.models import DiscoveryRun, InboxReceipt, InboxStatus, ProspectCandidate
    from app.identity.fake import BUSINESS_A
    from app.identity.tenant_context import TenantContext
    from app.inbox import InboxPayloadError, InboxSink

    source = "effortlesspromo-api"

    def ctx_a() -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=BUSINESS_A,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    def seed_candidate(session, identifier: str) -> str:
        run = DiscoveryRun(business_id=BUSINESS_A, status="completed")
        session.add(run)
        session.flush()
        cand = ProspectCandidate(
            business_id=BUSINESS_A,
            run_id=run.id,
            org_name="Synthetic Org",
            org_identifier=identifier,
            source_key="synthetic",
            source_ref="synthetic://1",
            trust_tier="official_registry",
            status="suggested",
        )
        session.add(cand)
        session.flush()
        return cand.id

    def handle(candidate_id: str, event_id: str, *, fail: bool = False) -> bool:
        """One idempotent-receiver unit of work. Returns True iff the local change was applied."""
        with ctx.session_factory() as session:
            sink = InboxSink.for_context(session, ctx_a())
            if sink.already_processed(event_source=source, event_id=event_id) is not None:
                return False
            cand = session.get(ProspectCandidate, candidate_id)
            cand.status = "accepted"
            sink.register(
                event_source=source, event_id=event_id, payload={"candidateId": candidate_id}
            )
            if fail:
                session.rollback()
                return False
            session.commit()
            return True

    out: dict = {}

    with ctx.session_factory() as session:
        c1 = seed_candidate(session, "SYN-1")
        c2 = seed_candidate(session, "SYN-2")
        session.commit()

    # 1 + 2) first delivery handles the event; a replay is a no-op via the fast path.
    applied_first = handle(c1, "evt-1")
    applied_replay = handle(c1, "evt-1")

    # 2b) a replay that skips the fast path is still rejected by the uniqueness constraint.
    dup_rejected = False
    with ctx.session_factory() as session:
        cand = session.get(ProspectCandidate, c1)
        cand.status = "re-accepted"
        InboxSink.for_context(session, ctx_a()).register(event_source=source, event_id="evt-1")
        try:
            session.commit()
        except IntegrityError:
            dup_rejected = True
            session.rollback()

    # 3) a failed delivery rolls back both the change and the receipt; 4) the retry applies it once.
    applied_fail = handle(c2, "evt-2", fail=True)
    with ctx.session_factory() as session:
        s2_after_fail = session.get(ProspectCandidate, c2).status
        receipts_after_fail = len(session.scalars(select(InboxReceipt)).all())
    applied_retry = handle(c2, "evt-2")

    # 5) refuse a raw-contact payload — nothing staged.
    refused = False
    refusal = ""
    with ctx.session_factory() as session:
        try:
            InboxSink.for_context(session, ctx_a()).register(
                event_source=source,
                event_id="evt-3",
                payload={"ownerEmail": "founder@acme.co.in"},
            )
        except InboxPayloadError as exc:
            refused = True
            refusal = str(exc)
        session.commit()

    # Inspect the actual persisted state.
    with ctx.session_factory() as session:
        s1 = session.get(ProspectCandidate, c1).status
        s2 = session.get(ProspectCandidate, c2).status
        rows = session.scalars(select(InboxReceipt).order_by(InboxReceipt.received_at)).all()

    print("\ndelivery effects:")
    print(f"  deliver E1 (first)  -> applied={applied_first}   (expected: True)")
    print(f"  deliver E1 (replay) -> applied={applied_replay}  (expected: False — fast-path no-op)")
    print(f"  re-register E1 no fast-path -> rejected={dup_rejected} (uniqueness constraint)")
    print(
        f"  deliver E2 (fails)  -> applied={applied_fail}  status after={s2_after_fail} "
        f"receipts after={receipts_after_fail} (both rolled back)"
    )
    print(f"  retry E2            -> applied={applied_retry}  (expected: True — clean re-run)")

    print("\ncandidate statuses:")
    print(f"  #1  status={s1}   (expected: accepted — applied exactly once despite the replay)")
    print(f"  #2  status={s2}   (expected: accepted — applied once on retry)")

    print(f"\ninbox_receipts rows persisted: {len(rows)} (expected: 2 — one per handled event)")
    for r in rows:
        print(
            f"  - source={r.event_source}  event_id={r.event_id}  status={r.status}  "
            f"payload={r.payload}"
        )
    print(f"\nraw-contact receipt refused: {refused}  ({refusal if refused else 'NOT REFUSED!'})")

    out["applied_first"] = applied_first
    out["replay_was_noop"] = applied_replay is False
    out["duplicate_rejected"] = dup_rejected
    out["failed_rolled_back"] = s2_after_fail == "suggested" and receipts_after_fail == 1
    out["retry_applied"] = applied_retry
    out["c1_status"] = s1
    out["c2_status"] = s2
    out["inbox_row_count"] = len(rows)
    out["all_processed"] = all(r.status == InboxStatus.PROCESSED.value for r in rows)
    out["raw_contact_refused"] = refused
    print(
        "\n(exactly-once local effect; failed processing retries cleanly; opaque payloads only; "
        "no paid action)"
    )
    return out


@register(
    "DCA-029",
    "integration envelope: accept valid; reject future-version/malformed/leaky before any mutation",
)
def _dca_029(ctx: CheckContext) -> dict:
    """Prove the versioned integration-event envelope validates an inbound event BEFORE a receiver
    touches state, and rejects an incompatible / malformed / non-opaque event without echoing its
    payload. A guarded receiver parses first and records a DCA-028 inbox receipt only on success, so
    for synthetic Business A:

      1. a VALID current-version event  -> validated, then recorded (one receipt);
      2. a FUTURE-version event         -> rejected (unsupported version), no receipt;
      3. a MALFORMED event (empty id / aggregate_version=0) -> rejected, no receipt;
      4. a NON-OPAQUE payload (raw contact) -> rejected, no receipt, and the raw value is NOT in the
         rejection message.

    Shows the actual inbox rows and each rejection message. No AI/paid action, no HTTP.
    """
    from sqlalchemy import select

    from app.db.models import InboxReceipt
    from app.identity.fake import BUSINESS_A
    from app.identity.tenant_context import TenantContext
    from app.inbox import InboxSink
    from app.integration import (
        CURRENT_ENVELOPE_VERSION,
        EnvelopeError,
        MalformedEnvelopeError,
        UnsupportedEnvelopeVersionError,
        parse_envelope,
    )

    source = "effortlesspromo-api"
    secret_email = "founder@acme.co.in"  # synthetic sentinel — must never appear in a rejection

    def ctx_a() -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=BUSINESS_A,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    def valid_raw(**overrides: object) -> dict:
        base: dict = {
            "schema_version": CURRENT_ENVELOPE_VERSION,
            "event_id": "evt-ok",
            "business_id": BUSINESS_A,
            "event_type": "candidate.accepted",
            "aggregate_version": 3,
            "occurred_at": "2026-01-01T12:00:00+00:00",
            "payload": {"candidateId": "cand-9"},
        }
        base.update(overrides)
        return base

    def guarded_receive(raw: object) -> bool:
        """Validate the envelope, then (only if valid) record a receipt. Raises before any mutation."""
        env = parse_envelope(raw)
        with ctx.session_factory() as session:
            InboxSink.for_context(session, ctx_a()).register(
                event_source=source,
                event_id=env.event_id,
                event_type=env.event_type,
                payload=env.payload,
            )
            session.commit()
        return True

    out: dict = {}

    # 1) valid event — validated then recorded
    recorded = guarded_receive(valid_raw())

    # 2) future-version, 3) malformed, 4) non-opaque payload — each rejected before mutation
    rejections: dict[str, str] = {}

    def attempt(label: str, raw: object, expected: type[EnvelopeError]) -> None:
        try:
            guarded_receive(raw)
            rejections[label] = "NOT REJECTED!"
        except expected as exc:
            rejections[label] = str(exc)

    attempt(
        "future_version",
        valid_raw(event_id="evt-future", schema_version=2),
        UnsupportedEnvelopeVersionError,
    )
    attempt(
        "malformed",
        valid_raw(event_id="", aggregate_version=0),
        MalformedEnvelopeError,
    )
    attempt(
        "non_opaque_payload",
        valid_raw(event_id="evt-leak", payload={"owner": secret_email}),
        MalformedEnvelopeError,
    )

    # Inspect the actual persisted state.
    with ctx.session_factory() as session:
        rows = session.scalars(select(InboxReceipt).order_by(InboxReceipt.received_at)).all()

    print("\nguarded receiver (validate -> only then record):")
    print(f"  valid current-version event -> recorded={recorded} (expected: True)")
    for label, message in rejections.items():
        print(f"  {label:20} -> rejected: {message}")

    print(f"\ninbox_receipts rows persisted: {len(rows)} (expected: 1 — only the valid event)")
    for r in rows:
        print(f"  - source={r.event_source}  event_id={r.event_id}  payload={r.payload}")

    leak = any(secret_email in message for message in rejections.values())
    print(
        f"\nsensitive-value leak in any rejection message: {leak} "
        f"({'LEAKED' if leak else 'clean — raw contact value never echoed'})"
    )

    out["valid_recorded"] = recorded
    out["inbox_row_count"] = len(rows)
    out["only_valid_recorded"] = len(rows) == 1 and rows[0].event_id == "evt-ok"
    out["rejections"] = rejections
    out["no_sensitive_echo"] = not leak
    print("\n(rejected before any mutation; opaque payloads only; no paid action)")
    return out


@register(
    "DCA-030",
    "webhook signatures: valid accepted; tampered/stale rejected; old+new keys work in overlap",
)
def _dca_030(ctx: CheckContext) -> dict:
    """Prove inbound webhook transport authentication: a validly-signed, fresh body is accepted; one
    flipped byte and a stale (replayed) timestamp are each rejected BEFORE a receiver records anything;
    and during a key-rotation OVERLAP both the old and the new key verify. A guarded receiver verifies
    the signature first, then records a DCA-028 inbox receipt only on success, so for synthetic
    Business A:

      1. a VALID current-key, fresh event   -> verified, then recorded (one receipt);
      2. a TAMPERED body (one byte changed)  -> rejected (signature mismatch), no receipt;
      3. a STALE timestamp (correctly signed, 1h old) -> rejected (replay guard), no receipt;
      4. OLD-key and NEW-key events in the overlap -> both verified and recorded.

    Shows the actual inbox rows and each rejection message, and checks the signing secret / signature
    never appear in a rejection message. No AI/paid action, no HTTP.
    """
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.db.models import InboxReceipt
    from app.identity.fake import BUSINESS_A
    from app.identity.tenant_context import TenantContext
    from app.inbox import InboxSink
    from app.integration import (
        CURRENT_ENVELOPE_VERSION,
        SignatureMismatchError,
        StaleWebhookError,
        WebhookAuthError,
        WebhookKey,
        WebhookKeyring,
        parse_envelope,
        sign_body,
        verify_webhook,
    )

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    secret_current = (
        "whsec_current_SENTINEL_0123456789"  # synthetic — must never appear in an error
    )
    secret_new = "whsec_new_SENTINEL_abcdefabcdef"
    old_key = WebhookKey("key-2025", secret_current)
    new_key = WebhookKey("key-2026", secret_new)
    single = WebhookKeyring([old_key])
    overlap = WebhookKeyring(
        [new_key, old_key]
    )  # new key current, old key kept through the overlap

    def ctx_a() -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=BUSINESS_A,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    def body_for(event_id: str) -> bytes:
        return json.dumps(
            {
                "schema_version": CURRENT_ENVELOPE_VERSION,
                "event_id": event_id,
                "business_id": BUSINESS_A,
                "event_type": "candidate.accepted",
                "aggregate_version": 1,
                "occurred_at": now.isoformat(),
                "payload": {"candidateId": "cand-9"},
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def guarded_receive(ring: WebhookKeyring, header: str, body: bytes, *, at: datetime) -> bool:
        """Verify the webhook signature FIRST, then parse the envelope and record a receipt."""
        verify_webhook(keyring=ring, header=header, body=body, now=at)  # raises before any mutation
        env = parse_envelope(json.loads(body))
        with ctx.session_factory() as session:
            InboxSink.for_context(session, ctx_a()).register(
                event_source="effortlesspromo-api",
                event_id=env.event_id,
                event_type=env.event_type,
                payload=env.payload,
            )
            session.commit()
        return True

    out: dict = {}
    rejections: dict[str, str] = {}

    def attempt(label: str, ring, header, body, expected, *, at) -> None:
        try:
            guarded_receive(ring, header, body, at=at)
            rejections[label] = "NOT REJECTED!"
        except expected as exc:
            rejections[label] = str(exc)

    # 1) valid current-key fresh event -> recorded
    ok_body = body_for("evt-ok")
    ok_header = sign_body(single, ok_body, timestamp=now)
    valid_recorded = guarded_receive(single, ok_header, ok_body, at=now)

    # 2) tampered body -> signature mismatch (header still signs the original body)
    tampered = body_for("evt-tampered")
    attempt("tampered_body", single, ok_header, tampered, SignatureMismatchError, at=now)

    # 3) stale (replayed) timestamp, correctly signed -> replay guard fires
    stale_body = body_for("evt-stale")
    stale_header = sign_body(single, stale_body, timestamp=now - timedelta(hours=1))
    attempt("stale_timestamp", single, stale_header, stale_body, StaleWebhookError, at=now)

    # 4) rotation overlap: an OLD-key and a NEW-key event both verify
    old_body = body_for("evt-old-key")
    old_header = sign_body(WebhookKeyring([old_key]), old_body, timestamp=now)
    new_body = body_for("evt-new-key")
    new_header = sign_body(WebhookKeyring([new_key]), new_body, timestamp=now)
    overlap_old = guarded_receive(overlap, old_header, old_body, at=now)
    overlap_new = guarded_receive(overlap, new_header, new_body, at=now)

    with ctx.session_factory() as session:
        rows = session.scalars(select(InboxReceipt).order_by(InboxReceipt.received_at)).all()

    print("\nguarded receiver (verify signature -> only then record):")
    print(f"  1. valid current-key event   -> recorded={valid_recorded} (expected True)")
    for label, message in rejections.items():
        print(f"  2/3. {label:16} -> rejected: {message}")
    print(f"  4. old-key event (overlap)   -> recorded={overlap_old}")
    print(f"     new-key event (overlap)   -> recorded={overlap_new}")

    recorded_ids = [r.event_id for r in rows]
    print(f"\ninbox_receipts rows persisted: {len(rows)} (expected 3: valid + old-key + new-key)")
    for r in rows:
        print(f"  - event_id={r.event_id}")
    print(f"recorded event ids: {recorded_ids}")

    leak = any(
        secret_current in m or secret_new in m or "SENTINEL" in m for m in rejections.values()
    )
    print(
        f"\nsigning secret leak in any rejection message: {leak} "
        f"({'LEAKED' if leak else 'clean — no secret echoed'})"
    )

    # Fail-closed: an unconfigured keyring refuses even a validly-signed body.
    closed = False
    try:
        verify_webhook(keyring=None, header=ok_header, body=ok_body, now=now)
    except WebhookAuthError as exc:
        closed = True
        print(f"\nunconfigured keyring -> fail-closed: {exc}")

    out["valid_recorded"] = valid_recorded
    out["rejections"] = rejections
    out["overlap_old_recorded"] = overlap_old
    out["overlap_new_recorded"] = overlap_new
    out["inbox_row_count"] = len(rows)
    out["recorded_ids"] = recorded_ids
    out["no_secret_echo"] = not leak
    out["unconfigured_fails_closed"] = closed
    print("\n(rejected before any mutation; no secret echoed; no paid action)")
    return out


@register(
    "DCA-031",
    "stale revisions: apply rev3 then delayed rev2 (unchanged); a gap is buffered pending, then reconciles",
)
def _dca_031(ctx: CheckContext) -> dict:
    """Prove revision-aware projection for synthetic Business A, over the DCA-029 envelope's
    ``aggregate_version``:

      1. apply revisions 1 -> 2 -> 3 in order         -> projection advances to 3;
      2. deliver a DELAYED revision 2                 -> rejected STALE, state UNCHANGED (rev 3 wins);
      3. deliver revision 5 while 4 is missing        -> buffered as a visible GAP (pending), not applied;
      4. deliver the missing revision 4               -> fills the gap and reconciles 5 in order.

    Shows the actual projection row (applied_version / status / state) and the buffered pending rows
    and computed missing versions at each step, so a human can see an older revision never overwrites
    a newer one and a gap is visible rather than guessed. Local synthetic SQLite; no AI/paid action.
    """
    from datetime import UTC, datetime

    from app.identity.fake import BUSINESS_A
    from app.identity.tenant_context import TenantContext
    from app.integration import new_envelope
    from app.projection import RevisionOutcome, RevisionProjector

    atype, aid = "consent", "consent-aggregate-1"

    def ctx_a() -> TenantContext:
        return TenantContext(
            subject="00000000-0000-4000-9000-0000000a11ce",
            business_id=BUSINESS_A,
            roles=("owner",),
            membership_version="fake-identity-v1",
        )

    def deliver(version: int, state: dict) -> RevisionOutcome:
        """A guarded receiver: mint+validate the envelope, then project its revision (one unit of work)."""
        env = new_envelope(
            event_id=f"evt-{version}",
            business_id=BUSINESS_A,
            event_type="consent.updated",
            aggregate_version=version,
            occurred_at=datetime(2026, 1, 1, 12, version, 0, tzinfo=UTC),
            payload=state,
        )
        with ctx.session_factory() as session:
            projector = RevisionProjector.for_context(session, ctx_a())
            outcome = projector.apply(
                aggregate_type=atype,
                aggregate_id=aid,
                version=env.aggregate_version,
                state=dict(env.payload),
                event_id=env.event_id,
                occurred_at=env.occurred_at,
            )
            session.commit()
        return outcome

    def snapshot(label: str) -> dict:
        with ctx.session_factory() as session:
            projector = RevisionProjector.for_context(session, ctx_a())
            proj = projector.projection(aggregate_type=atype, aggregate_id=aid)
            pending = [
                p.aggregate_version
                for p in projector.pending(aggregate_type=atype, aggregate_id=aid)
            ]
            missing = projector.gaps(aggregate_type=atype, aggregate_id=aid)
        state = proj.state if proj else {}
        applied = proj.applied_version if proj else 0
        status = proj.status if proj else "(none)"
        print(
            f"  [{label}] applied_version={applied} status={status} state={state} "
            f"pending={pending} missing_gap={missing}"
        )
        return {
            "applied_version": applied,
            "status": status,
            "state": state,
            "pending": pending,
            "missing": missing,
        }

    out: dict = {}
    print("\n1) apply revisions 1 -> 2 -> 3 in order:")
    for v in (1, 2, 3):
        o = deliver(v, {"rev": v, "consent": "granted"})
        print(f"  deliver rev {v} -> {o.decision.value} (applied_version={o.applied_version})")
    out["after_ordered"] = snapshot("after 1,2,3")

    print("\n2) deliver a DELAYED older revision 2 (must NOT overwrite rev 3):")
    stale = deliver(2, {"rev": 2, "consent": "withdrawn"})
    print(f"  deliver rev 2 (delayed) -> {stale.decision.value} ({stale.reason})")
    out["stale_decision"] = stale.decision.value
    out["after_stale"] = snapshot("after delayed rev 2")

    print("\n3) deliver revision 5 while revision 4 is missing (a GAP - buffered, not applied):")
    gap = deliver(5, {"rev": 5})
    print(f"  deliver rev 5 -> {gap.decision.value} ({gap.reason})")
    out["gap_decision"] = gap.decision.value
    out["gap_missing"] = list(gap.missing)
    out["after_gap"] = snapshot("after rev 5 (gap)")

    print("\n4) deliver the missing revision 4 (fills the gap, reconciles 5 in order):")
    filled = deliver(4, {"rev": 4})
    print(
        f"  deliver rev 4 -> {filled.decision.value} "
        f"(applied_version={filled.applied_version}, reconciled={list(filled.reconciled)})"
    )
    out["reconciled"] = list(filled.reconciled)
    out["after_reconcile"] = snapshot("after rev 4 (reconciled)")

    stale_preserved = out["after_stale"]["applied_version"] == 3 and out["after_stale"][
        "state"
    ] == {"rev": 3, "consent": "granted"}
    gap_visible = out["after_gap"]["applied_version"] == 3 and out["after_gap"]["pending"] == [5]
    reconciled_ok = (
        out["after_reconcile"]["applied_version"] == 5
        and out["after_reconcile"]["status"] == "current"
        and out["after_reconcile"]["pending"] == []
    )
    print(
        "\nchecks: "
        f"older_revision_did_not_overwrite={stale_preserved}  "
        f"gap_visible_not_guessed={gap_visible}  "
        f"reconciled_in_order={reconciled_ok}"
    )
    out["older_revision_did_not_overwrite"] = stale_preserved
    out["gap_visible_not_guessed"] = gap_visible
    out["reconciled_in_order"] = reconciled_ok
    print(
        "\n(an older revision never overwrote a newer projection; the gap was visible, not guessed; no paid action)"
    )
    return out


@register(
    "DCA-032",
    "platform HTTP transport: success/timeout/unauthorized via a fake host; off-allowlist refused",
)
def _dca_032(ctx: CheckContext) -> dict:
    """Drive the platform HTTP transport against a FAKE host (``httpx.MockTransport``, no network) and
    show, side by side, the real transport outputs for:

      1. a success                         -> SUCCESS result, exactly one call on the wire;
      2. a 401 unauthorized                -> UNAUTHORIZED result (classified, not raised);
      3. a timeout (connect AND read)      -> both classify identically as a stable ``timeout``;
      4. an off-allowlist path             -> REFUSED before any I/O (the fake host is never called).

    Also proves the scoped credential is sent as ``Authorization: Bearer <cred>`` yet never appears in
    the emitted logs (central DCA-012 redaction). No real network, no AI, no paid action; read-only.
    """
    import io
    import json as jsonlib
    import logging

    import httpx

    from app.clients.platform_transport import (
        PlatformEndpointNotAllowed,
        PlatformHttpTransport,
        PlatformTransportError,
    )
    from app.core.logging import configure_logging

    base = "https://platform.internal/svc"
    cred = "scoped-platform-credential-SENTINEL-xyz"  # synthetic, local-only

    class FakeHost:
        """Records every request it is asked to serve and returns a scripted response/exception.

        The ``calls`` counter is the state-change probe: a refused (allowlisted) request must never
        reach here, so the counter stays put on a denial.
        """

        def __init__(self) -> None:
            self.calls = 0
            self.last: httpx.Request | None = None
            self.script: object = httpx.Response(200, json={"ok": True})

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            self.last = request
            if isinstance(self.script, Exception):
                raise self.script
            assert isinstance(self.script, httpx.Response)
            return self.script

    host = FakeHost()

    # Capture the dca log stream so we can grep it for the credential literal afterwards.
    buffer = io.StringIO()
    configure_logging(
        type(ctx.settings)(
            _env_file=None,  # type: ignore[call-arg]
            environment="development",
            database_url="sqlite+pysqlite:///:memory:",
            platform_base_url=base,
            platform_service_credential=cred,
        ),
        stream=buffer,
    )

    transport = PlatformHttpTransport(base, cred, transport=httpx.MockTransport(host))
    out: dict = {}

    print(f"\nplatform transport allowlisted to: {base}")
    print("(fake host via httpx.MockTransport — no real network, no AI, no paid action)\n")

    # 1) success
    host.script = httpx.Response(200, json={"tenant": "A", "status": "ok"})
    before = host.calls
    ok = transport.request("GET", "tenants/A", correlation_id="trace-ok")
    print(
        f"1) GET tenants/A (host 200)        -> class={ok.response_class.value:12} "
        f"status={ok.status_code} url={ok.url} calls={host.calls - before}"
    )
    out["success"] = {
        "class": ok.response_class.value,
        "status": ok.status_code,
        "url": ok.url,
        "json": ok.json,
    }
    sent_auth = host.last.headers.get("Authorization") if host.last else None
    sent_cid = host.last.headers.get("X-Correlation-Id") if host.last else None
    print(
        f"   credential header sent          -> Authorization={sent_auth!r} "
        f"(== 'Bearer <cred>'); X-Correlation-Id={sent_cid!r}"
    )
    out["credential_header_is_bearer"] = sent_auth == f"Bearer {cred}"
    out["correlation_propagated"] = sent_cid == "trace-ok"

    # 2) unauthorized (classified, not raised)
    host.script = httpx.Response(401, json={"error": "nope"})
    denied = transport.request("GET", "tenants/A", correlation_id="trace-401")
    print(
        f"2) GET tenants/A (host 401)        -> class={denied.response_class.value:12} "
        f"status={denied.status_code} (returned, not raised)"
    )
    out["unauthorized"] = {"class": denied.response_class.value, "status": denied.status_code}

    # 3) timeout — connect-phase AND read-phase both classify identically (stable)
    timeout_kinds: dict[str, str] = {}
    for label, exc in (
        ("connect", httpx.ConnectTimeout("connect timed out")),
        ("read", httpx.ReadTimeout("read timed out")),
    ):
        host.script = exc
        try:
            transport.request("GET", "tenants/A")
            timeout_kinds[label] = "UNEXPECTED SUCCESS"
        except PlatformTransportError as err:
            timeout_kinds[label] = err.kind.value
            leaked = cred in str(err)
            print(
                f"3) GET tenants/A ({label}-timeout)  -> raised {type(err).__name__} "
                f"kind={err.kind.value!r} (credential in message? {leaked})"
            )
    out["timeout_kinds"] = timeout_kinds
    out["timeout_classification_stable"] = (
        timeout_kinds.get("connect") == timeout_kinds.get("read") == "timeout"
    )

    # 4) off-allowlist path — refused before any I/O (no state change on the wire)
    host.script = httpx.Response(200, json={"should": "never be reached"})
    calls_before = host.calls
    refused = False
    for bad in ("https://evil.example/steal", "//evil.example/x", "../../escape"):
        try:
            transport.request("GET", bad)
        except PlatformEndpointNotAllowed as err:
            refused = True
            print(f"4) GET {bad:32} -> REFUSED ({err}) — no call reached the host")
    out["off_allowlist_refused"] = refused
    out["off_allowlist_calls_on_wire"] = host.calls - calls_before

    transport.close()

    # Credential redaction over everything the transport logged.
    raw = buffer.getvalue()
    logging.getLogger("dca.platform").info("explicit leaky line with %s", cred)
    raw = buffer.getvalue()
    lines = [jsonlib.loads(line) for line in raw.splitlines() if line.strip()]
    present = cred in raw
    print("\nemitted transport log lines (credential must be redacted):")
    for line in lines[-4:]:
        print(f"  {jsonlib.dumps(line)}")
    print(f"\ncredential literal present in logs? {present} (expect False)")
    out["credential_in_logs"] = present

    print(
        "\nchecks: "
        f"success={out['success']['class'] == 'success'}  "
        f"unauthorized={out['unauthorized']['class'] == 'unauthorized'}  "
        f"timeout_stable={out['timeout_classification_stable']}  "
        f"off_allowlist_refused={refused and out['off_allowlist_calls_on_wire'] == 0}  "
        f"credential_redacted={not present}"
    )
    print(
        "\n(every call stayed within the allowlisted endpoint; the credential never left a log line; "
        "no real network, no AI, no paid action, no state changed)"
    )
    return out


@register(
    "DCA-033",
    "fake contract-test server: simulate success/timeout/unreachable, 404 on an unknown contract, "
    "and refuse production",
)
def _dca_033(ctx: CheckContext) -> dict:
    """Drive the real platform transport against the reusable FAKE CONTRACT SERVER (DCA-033), showing:

      1. the explicit non-production synthetic-mode banner;
      2. a registered route served as a SUCCESS;
      3. that SAME route switched to a TIMEOUT response (the manual-test step) — classified timeout;
      4. a route configured UNREACHABLE — classified unreachable;
      5. an UNKNOWN route -> a deliberate 404 'no_fixture_configured' (no invented contract), recorded
         in the call log as unmatched;
      6. that ``build_fake_contract_server`` REFUSES production settings.

    No real network, no AI, no paid action; nothing is written. Uses ``httpx.MockTransport`` only.
    """
    from app.clients.platform_transport import (
        PlatformHttpTransport,
        PlatformTransportError,
        TransportErrorKind,
    )
    from app.core.config import Settings
    from app.testing import (
        FakeServerForbiddenInProduction,
        FixtureOutcome,
        build_fake_contract_server,
    )

    base = "https://platform.internal/svc"
    cred = "scoped-platform-credential-SENTINEL-xyz"  # synthetic, local-only
    out: dict = {}

    # The harness must refuse production selection — build it from NON-production settings here. The
    # transport base URL is mounted under ``/svc``, so the server strips that prefix before matching.
    server = build_fake_contract_server(ctx.settings, base_path="/svc")
    print(f"\n{server.banner()}\n")
    out["banner"] = server.banner()
    out["banner_is_non_production"] = "NON-PRODUCTION" in server.banner()

    transport = PlatformHttpTransport(base, cred, transport=server.as_transport())

    # 2) success on a registered route
    server.set_route("GET", "/tenants/A", FixtureOutcome.response(200, json={"tenant": "A"}))
    ok = transport.request("GET", "tenants/A", correlation_id="trace-ok")
    print(
        f"1) GET tenants/A (registered 200)  -> class={ok.response_class.value:12} "
        f"status={ok.status_code} json={ok.json}"
    )
    out["success_class"] = ok.response_class.value

    # 3) switch the SAME route to a timeout (the manual-test step)
    server.set_route("GET", "/tenants/A", FixtureOutcome.timeout())
    try:
        transport.request("GET", "tenants/A")
        out["timeout_kind"] = "UNEXPECTED SUCCESS"
    except PlatformTransportError as err:
        out["timeout_kind"] = err.kind.value
        print(
            f"2) GET tenants/A (switched->timeout) -> raised {type(err).__name__} "
            f"kind={err.kind.value!r} (credential in message? {cred in str(err)})"
        )

    # 4) unreachable
    server.set_route("GET", "/down", FixtureOutcome.unreachable())
    try:
        transport.request("GET", "down")
        out["unreachable_kind"] = "UNEXPECTED SUCCESS"
    except PlatformTransportError as err:
        out["unreachable_kind"] = err.kind.value
        print(f"3) GET down (unreachable)          -> raised kind={err.kind.value!r}")

    # 5) an UNKNOWN contract -> deliberate 404, recorded as unmatched (no invented contract)
    calls_before = server.call_count
    unknown = transport.request("GET", "suppression/check")
    last = server.last_call
    print(
        f"4) GET suppression/check (no fixture) -> status={unknown.status_code} "
        f"error={unknown.json.get('error') if unknown.json else None!r} "
        f"recorded_unmatched={last.matched is False if last else None}"
    )
    out["unknown_status"] = unknown.status_code
    out["unknown_error"] = unknown.json.get("error") if unknown.json else None
    out["unknown_recorded_unmatched"] = bool(last and last.matched is False)
    out["unknown_added_one_call"] = server.call_count - calls_before == 1

    transport.close()

    # 6) production refusal
    prod_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        database_url="sqlite+pysqlite:///:memory:",
    )
    try:
        build_fake_contract_server(prod_settings)
        out["refuses_production"] = False
    except FakeServerForbiddenInProduction as err:
        out["refuses_production"] = True
        print(f"5) build under production settings  -> REFUSED ({type(err).__name__})")

    print(
        "\nchecks: "
        f"banner={out['banner_is_non_production']}  "
        f"success={out['success_class'] == 'success'}  "
        f"timeout_stable={out['timeout_kind'] == TransportErrorKind.TIMEOUT.value}  "
        f"unreachable={out['unreachable_kind'] == TransportErrorKind.UNREACHABLE.value}  "
        f"no_invented_contract={out['unknown_status'] == 404 and out['unknown_recorded_unmatched']}  "
        f"refuses_production={out['refuses_production']}"
    )
    print(
        "\n(no real network, no AI, no paid action; the harness never invented a missing contract "
        "and refuses production selection)"
    )
    return out


@register(
    "DCA-034",
    "idempotent outbox publisher: stage a PENDING event, lose its acknowledgment, retry, and show "
    "the fake host recorded ONE logical event (same stable Idempotency-Key)",
)
def _dca_034(ctx: CheckContext) -> dict:
    """Drive the real DCA-034 outbox publisher against the DCA-033 FAKE CONTRACT SERVER, showing:

      1. a PENDING outbox message staged by the DCA-027 sink (its stable event id);
      2. publish with the acknowledgment LOST (transport timeout) -> the row is UNCHANGED: still
         PENDING, no delivered_at, the SAME event id — never a fresh key (the headline criterion);
      3. retry (host now acknowledges) -> the row flips to DELIVERED;
      4. BOTH attempts carried the SAME Idempotency-Key (the stable event id), so the fake host saw
         ONE logical event across two HTTP calls;
      5. the publisher is DISABLED (fail-closed) when no relay path is configured.

    Local synthetic only: the publisher's transport is an ``httpx.MockTransport`` over the fake
    server, so no real network/AI/paid action occurs and nothing leaves the box.
    """
    from sqlalchemy import select

    from app.core.config import Settings
    from app.db.models import OutboxMessage, OutboxStatus
    from app.identity.tenant_context import TenantContext
    from app.outbox import OutboxSink, PublishOutcome, build_outbox_publisher
    from app.testing import FixtureOutcome, build_fake_contract_server

    A = "00000000-0000-4000-8000-00000000000a"
    base = "https://platform.internal/svc"
    cred = "scoped-platform-credential-SENTINEL-xyz"  # synthetic, local-only
    path = "/outbox/events"
    out: dict = {}

    def _key(call) -> str | None:
        for name, value in call.headers.items():
            if name.lower() == "idempotency-key":
                return value
        return None

    # A non-production settings profile with the platform transport + relay path configured, so the
    # publisher is BUILT; the transport is pointed at the fake server (no socket ever opens).
    pub_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=ctx.settings.environment,
        database_url="sqlite+pysqlite:///:memory:",  # placeholder; the real factory is ctx's
        platform_base_url=base,
        platform_service_credential=cred,
        platform_outbox_publish_path=path,
    )
    server = build_fake_contract_server(ctx.settings, base_path="/svc")
    print(f"\n{server.banner()}\n")

    # 1) stage a PENDING outbox intent for synthetic Business A.
    ctx_a = TenantContext(
        subject="00000000-0000-4000-9000-0000000a11ce",
        business_id=A,
        roles=("owner",),
        membership_version="v1",
    )
    with ctx.session_factory() as session:
        row = OutboxSink.for_context(session, ctx_a).enqueue(
            event_type="candidate.accepted",
            dedup_key="cand-123:accept",
            payload={"candidateId": "cand-123", "runId": "run-9"},
        )
        session.commit()
        event_id = row.event_id
    out["event_id"] = event_id
    print(f"1) staged PENDING outbox event  -> event_id={event_id}")

    publisher = build_outbox_publisher(
        pub_settings,
        ctx.session_factory,  # type: ignore[arg-type]
        transport=server.as_transport(),
    )
    assert publisher is not None

    # 2) publish with the acknowledgment LOST (transport timeout).
    server.set_route("POST", path, FixtureOutcome.timeout())
    first = publisher.publish_pending(A)[0]
    out["first_outcome"] = first.outcome.value
    out["first_key"] = _key(server.last_call)
    with ctx.session_factory() as check:
        r = check.scalars(select(OutboxMessage)).one()
        out["after_timeout_status"] = r.status
        out["after_timeout_delivered_at"] = r.delivered_at
        out["after_timeout_event_id"] = r.event_id
    print(
        f"2) publish (ack LOST -> timeout) -> outcome={first.outcome.value} "
        f"row.status={out['after_timeout_status']} delivered_at={out['after_timeout_delivered_at']} "
        f"event_id_unchanged={out['after_timeout_event_id'] == event_id}"
    )

    # 3) retry: the host acknowledges now.
    server.set_route("POST", path, FixtureOutcome.response(202, json={"accepted": True}))
    second = publisher.publish_pending(A)[0]
    out["second_outcome"] = second.outcome.value
    out["second_key"] = _key(server.last_call)
    with ctx.session_factory() as check:
        r = check.scalars(select(OutboxMessage)).one()
        out["after_retry_status"] = r.status
        out["after_retry_event_id"] = r.event_id
    print(
        f"3) retry (host acknowledges)     -> outcome={second.outcome.value} "
        f"row.status={out['after_retry_status']}"
    )

    # 4) one logical event across two HTTP attempts: same stable Idempotency-Key both times.
    posts = [c for c in server.calls if c.method == "POST"]
    keys = {_key(c) for c in posts}
    out["post_attempts"] = len(posts)
    out["distinct_idempotency_keys"] = sorted(k for k in keys if k)
    print(
        f"4) fake host saw {len(posts)} HTTP POSTs, Idempotency-Key(s)={out['distinct_idempotency_keys']} "
        f"-> one logical event={keys == {event_id}}"
    )

    # 5) disabled-by-default: no relay path => no publisher is built (fail-closed denial).
    disabled_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=ctx.settings.environment,
        database_url="sqlite+pysqlite:///:memory:",
        platform_base_url=base,
        platform_service_credential=cred,
        # platform_outbox_publish_path deliberately unset
    )
    disabled = build_outbox_publisher(disabled_settings, ctx.session_factory)  # type: ignore[arg-type]
    out["disabled_without_relay_path"] = disabled is None
    print(f"5) no relay path configured      -> publisher built? {disabled is not None} (disabled)")

    print(
        "\nchecks: "
        f"timeout_retryable={out['first_outcome'] == PublishOutcome.RETRY_TIMEOUT.value}  "
        f"no_state_change_on_timeout="
        f"{out['after_timeout_status'] == OutboxStatus.PENDING.value and out['after_timeout_delivered_at'] is None}  "
        f"same_identity={out['after_timeout_event_id'] == event_id}  "
        f"retry_delivered="
        f"{out['second_outcome'] == PublishOutcome.DELIVERED.value and out['after_retry_status'] == OutboxStatus.DELIVERED.value}  "
        f"one_logical_event={keys == {event_id}}  "
        f"disabled_without_contract={out['disabled_without_relay_path']}"
    )
    print(
        "\n(no real network, no AI, no paid action; the publisher only rides the one allowlisted "
        "platform transport and stays disabled without a relay contract)"
    )
    return out


@register(
    "DCA-035",
    "request idempotency: repeat an A write (replay, no 2nd change), same key + different body "
    "(409), then reuse the key for B (no cross-tenant collision)",
)
def _dca_035(ctx: CheckContext) -> dict:
    """Drive the real DCA-035 request idempotency over the HTTP API against synthetic A/B, following
    the developer manual-test steps:

      1. register A and B;
      2. PUT A's config with an Idempotency-Key -> the first result;
      3. REPEAT the exact request + key -> the SAME result, served as a replay (no second state
         change: an out-of-band write in between is NOT overwritten by the replay);
      4. same key, DIFFERENT body -> rejected 409 'idempotency_key_reuse', changing nothing;
      5. REUSE the same key for B -> B's own result (the key does not collide across tenants).

    Local synthetic only (AI disabled, ephemeral DB); every call is shown with its real body so a
    human can compare against the acceptance criteria rather than a bare PASS.
    """
    from fastapi.testclient import TestClient

    from app.idempotency import IDEMPOTENCY_HEADER
    from app.main import create_app

    biz_a = "11111111-1111-1111-1111-111111111111"
    biz_b = "22222222-2222-2222-2222-222222222222"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}
    key = "cfg-key-1"

    def keyed() -> dict:
        return {**auth, IDEMPOTENCY_HEADER: key}

    def config(client, biz) -> dict:
        return client.get(f"/v1/tenants/{biz}/config", headers=auth).json()

    app = create_app(ctx.settings)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        for biz in (biz_a, biz_b):
            client.post("/v1/tenants", json={"businessId": biz}, headers=auth)

        body = {"dataMode": "suggest_platform_data", "enabled": True}

        # 2) first write with the key
        first = client.put(f"/v1/tenants/{biz_a}/config", json=body, headers=keyed())
        print(
            f"\n1) PUT A config (key={key})            -> HTTP {first.status_code} {first.json()}"
        )
        out["first"] = {"status": first.status_code, "body": first.json()}

        # out-of-band write (no key) moves A's config elsewhere, to prove the replay does NOT re-run
        client.put(
            f"/v1/tenants/{biz_a}/config",
            json={"dataMode": "my_data_only", "enabled": False},
            headers=auth,
        )
        moved = config(client, biz_a)
        print(f"   out-of-band write (no key) moves A    -> dataMode={moved['dataMode']}")

        # 3) repeat the exact first request + key -> replay of the first result, no re-run
        replay = client.put(f"/v1/tenants/{biz_a}/config", json=body, headers=keyed())
        after = config(client, biz_a)
        print(
            f"2) REPEAT A config (same key+body)     -> HTTP {replay.status_code} {replay.json()} "
            f"replay_header={replay.headers.get('Idempotent-Replay')}"
        )
        print(
            f"   A config after replay                 -> dataMode={after['dataMode']} "
            f"(unchanged by the replay - handler did not run)"
        )
        out["replay"] = {
            "status": replay.status_code,
            "body": replay.json(),
            "is_replay": replay.headers.get("Idempotent-Replay") == "true",
            "matched_first_result": replay.json() == first.json(),
            "no_second_change": after["dataMode"] == "my_data_only" and after["enabled"] is False,
        }

        # 4) same key, different body -> rejected, nothing changed
        before = config(client, biz_a)
        reuse = client.put(
            f"/v1/tenants/{biz_a}/config", json={"dataMode": "my_data_only"}, headers=keyed()
        )
        print(
            f"3) same key, DIFFERENT body            -> HTTP {reuse.status_code} "
            f"{reuse.json()['error']['message']}"
        )
        out["conflict"] = {
            "status": reuse.status_code,
            "message": reuse.json()["error"]["message"],
            "changed_nothing": config(client, biz_a) == before,
        }

        # 5) reuse the SAME key for B -> B's own result, no collision with A
        b_resp = client.put(f"/v1/tenants/{biz_b}/config", json=body, headers=keyed())
        print(
            f"4) REUSE key for B                     -> HTTP {b_resp.status_code} {b_resp.json()} "
            f"replay_header={b_resp.headers.get('Idempotent-Replay')}"
        )
        out["cross_tenant"] = {
            "status": b_resp.status_code,
            "business_id": b_resp.json().get("businessId"),
            "is_own_result": b_resp.json().get("businessId") == biz_b,
            "not_a_replay": b_resp.headers.get("Idempotent-Replay") is None,
        }
    finally:
        app.state.engine.dispose()

    print(
        "\nchecks: "
        f"replay_same_result={out['replay']['matched_first_result'] and out['replay']['is_replay']}  "
        f"no_second_state_change={out['replay']['no_second_change']}  "
        f"reuse_rejected={out['conflict']['status'] == 409 and out['conflict']['changed_nothing']}  "
        f"no_cross_tenant_collision="
        f"{out['cross_tenant']['is_own_result'] and out['cross_tenant']['not_a_replay']}"
    )
    print(
        "\n(local synthetic only; no AI/paid action; the rejected reuse and the replay both changed "
        "no state beyond the first accepted write)"
    )
    return out


@register(
    "DCA-036",
    "versioned profile drafts: save two versions (prior preserved), unknowns retained, no confirmed "
    "profile created, and the confirm capability is denied without changing state",
)
def _dca_036(ctx: CheckContext) -> dict:
    """Drive the real DCA-036 versioned business-profile drafts over the HTTP API against synthetic A,
    following the developer manual-test steps:

      1. register A;
      2. save draft v1 (some aspects left empty) -> version 1, status 'draft', empty aspects retained
         in 'unknowns';
      3. save a SECOND draft -> version 2, and show v1 is still retrievable and UNCHANGED (a new draft
         preserves prior versions, it does not overwrite);
      4. GET the confirmed profile -> none exists (saving drafts never created a confirmed profile);
      5. attempt to confirm v1 -> 403 'profile_confirmation_disabled', changing NO state (still no
         confirmed profile, draft list unchanged).

    Local synthetic only (AI disabled, ephemeral DB); every call is shown with its real body so a human
    can compare against the acceptance criteria rather than a bare PASS.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    biz_a = "11111111-1111-1111-1111-111111111111"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}
    base = f"/v1/tenants/{biz_a}/profile"

    app = create_app(ctx.settings)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        client.post("/v1/tenants", json={"businessId": biz_a}, headers=auth)

        # 2) first draft — products & business type known; the rest deliberately left empty
        v1_body = {
            "products": ["artisan tea"],
            "businessType": "retail",
            "valueProposition": "",
            "languages": [],
            "confirmedFacts": [],
        }
        v1 = client.post(f"{base}/drafts", json=v1_body, headers=auth)
        print(f"\n1) save draft v1                 -> HTTP {v1.status_code} {v1.json()}")
        out["v1"] = {"status": v1.status_code, "body": v1.json()}

        # 3) second draft — a NEW version; v1 must remain
        v2 = client.post(
            f"{base}/drafts",
            json={**v1_body, "businessType": "wholesale", "languages": ["en", "hi"]},
            headers=auth,
        )
        print(f"2) save draft v2                 -> HTTP {v2.status_code} {v2.json()}")
        listed = client.get(f"{base}/drafts", headers=auth).json()
        v1_after = client.get(f"{base}/drafts/1", headers=auth).json()
        print(f"   versions on record            -> {[r['version'] for r in listed]}")
        print(
            f"   v1 after v2 saved             -> businessType={v1_after['businessType']} "
            f"unknowns={v1_after['unknowns']} (unchanged; prior version preserved)"
        )
        out["versions"] = [r["version"] for r in listed]
        out["v1_preserved"] = {
            "business_type": v1_after["businessType"],
            "unknowns": v1_after["unknowns"],
        }

        # 4) no confirmed profile was created automatically
        confirmed_before = client.get(f"{base}/confirmed", headers=auth).json()
        print(f"3) GET confirmed profile         -> {confirmed_before}")
        out["confirmed_before"] = confirmed_before

        # 5) the confirm capability is disabled — denial with no state change
        drafts_before = client.get(f"{base}/drafts", headers=auth).json()
        denied = client.post(f"{base}/drafts/1/confirm", headers=auth)
        confirmed_after = client.get(f"{base}/confirmed", headers=auth).json()
        drafts_after = client.get(f"{base}/drafts", headers=auth).json()
        print(
            f"4) attempt confirm v1            -> HTTP {denied.status_code} "
            f"({denied.json()['error']['message']})"
        )
        print(
            f"   confirmed profile after deny  -> confirmed={confirmed_after['confirmed']} "
            f"(still none); drafts unchanged={drafts_after == drafts_before}"
        )
        out["confirm_denied"] = {
            "status": denied.status_code,
            "message": denied.json()["error"]["message"],
            "still_no_confirmed": confirmed_after["confirmed"] is False,
            "drafts_unchanged": drafts_after == drafts_before,
        }
    finally:
        app.state.engine.dispose()

    print(
        "\nchecks: "
        f"two_versions={out['versions'] == [1, 2]}  "
        f"prior_preserved={out['v1_preserved']['business_type'] == 'retail'}  "
        "unknowns_retained="
        f"{set(out['v1_preserved']['unknowns']) == {'value_proposition', 'languages', 'confirmed_facts'}}  "
        f"no_auto_confirmed={out['confirmed_before']['confirmed'] is False}  "
        f"confirm_denied_no_change="
        f"{out['confirm_denied']['status'] == 403 and out['confirm_denied']['still_no_confirmed'] and out['confirm_denied']['drafts_unchanged']}"
    )
    print(
        "\n(local synthetic only; no AI/paid action; the denied confirm and the second draft changed "
        "no prior version and created no confirmed profile)"
    )
    return out


@register(
    "DCA-037",
    "import an authorized platform profile snapshot: A/B import distinct provenance-bearing drafts; "
    "the import creates no confirmed profile; the disabled-contract and nothing-to-import denials "
    "change no state",
)
def _dca_037(ctx: CheckContext) -> dict:
    """Drive the real DCA-037 platform-profile import over the HTTP API against synthetic A and B,
    following the developer manual-test steps:

      1. register A and B;
      2. import A -> a NEW draft version, status 'draft', carrying the platform's facts + Brand KB
         facts and provenance (source + source_version); import B -> a DISTINCT draft (different
         products/business type), proving per-business scope;
      3. GET the confirmed profile -> none exists (importing a draft never creates a confirmed profile);
      4. with the live contract disabled (production, B1) the import fails closed (503
         'profile_import_contract_disabled') and stages NO draft;
      5. with a profile-less fake (nothing on record) the import is 404 'platform_profile_not_found'
         and stages NO draft.

    Local synthetic only (AI disabled, ephemeral DB); the contract is the deterministic fake (no
    host-table access). Every call is shown with its real body so a human can compare against the
    acceptance criteria rather than a bare PASS.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.profiles import FakePlatformProfileClient
    from app.services.profile_importer import (
        REASON_IMPORT_CONTRACT_DISABLED,
        REASON_PROFILE_NOT_FOUND,
    )

    # The fake's synthetic businesses (A/B) have fixture profiles on record.
    biz_a = "00000000-0000-4000-8000-00000000000a"
    biz_b = "00000000-0000-4000-8000-00000000000b"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}

    app = create_app(ctx.settings)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        for biz in (biz_a, biz_b):
            client.post("/v1/tenants", json={"businessId": biz}, headers=auth)

        # 2) import A and B — distinct, provenance-bearing drafts
        a = client.post(f"/v1/tenants/{biz_a}/profile/import", headers=auth)
        b = client.post(f"/v1/tenants/{biz_b}/profile/import", headers=auth)
        a_body, b_body = a.json(), b.json()
        print(f"\n1) import A -> HTTP {a.status_code}")
        print(
            f"   A: version={a_body['version']} status={a_body['status']} "
            f"source={a_body['source']} sourceVersion={a_body['sourceVersion']}"
        )
        print(f"      products={a_body['products']} facts={a_body['confirmedFacts']}")
        print(f"2) import B -> HTTP {b.status_code}")
        print(
            f"   B: version={b_body['version']} status={b_body['status']} "
            f"businessType={b_body['businessType']} products={b_body['products']}"
        )
        out["a"] = {"status": a.status_code, "body": a_body}
        out["b"] = {"status": b.status_code, "body": b_body}

        # 3) importing created no confirmed profile
        confirmed = client.get(f"/v1/tenants/{biz_a}/profile/confirmed", headers=auth).json()
        print(f"3) GET A confirmed profile -> confirmed={confirmed['confirmed']} (still none)")
        out["confirmed"] = confirmed

        # 4) disabled contract (production): import fails closed, stages nothing
        drafts_before = client.get(f"/v1/tenants/{biz_a}/profile/drafts", headers=auth).json()
        app.state.platform_profile_client = None
        disabled = client.post(f"/v1/tenants/{biz_a}/profile/import", headers=auth)
        drafts_after = client.get(f"/v1/tenants/{biz_a}/profile/drafts", headers=auth).json()
        print(
            f"4) import with contract DISABLED -> HTTP {disabled.status_code} "
            f"({disabled.json()['error']['message']}); drafts unchanged="
            f"{drafts_after == drafts_before}"
        )
        out["contract_disabled"] = {
            "status": disabled.status_code,
            "message": disabled.json()["error"]["message"],
            "drafts_unchanged": drafts_after == drafts_before,
        }

        # 5) nothing on record: import is 404, stages nothing
        app.state.platform_profile_client = FakePlatformProfileClient(fixtures={})
        none_before = client.get(f"/v1/tenants/{biz_b}/profile/drafts", headers=auth).json()
        empty = client.post(f"/v1/tenants/{biz_b}/profile/import", headers=auth)
        none_after = client.get(f"/v1/tenants/{biz_b}/profile/drafts", headers=auth).json()
        print(
            f"5) import with NO profile on record -> HTTP {empty.status_code} "
            f"({empty.json()['error']['message']}); drafts unchanged={none_after == none_before}"
        )
        out["nothing_to_import"] = {
            "status": empty.status_code,
            "message": empty.json()["error"]["message"],
            "drafts_unchanged": none_after == none_before,
        }
    finally:
        app.state.engine.dispose()

    print(
        "\nchecks: "
        f"a_imported={out['a']['status'] == 201 and out['a']['body']['status'] == 'draft'}  "
        f"provenance_retained={out['a']['body']['sourceVersion'] != ''}  "
        f"distinct_ab={out['a']['body']['products'] != out['b']['body']['products']}  "
        f"no_confirmed={out['confirmed']['confirmed'] is False}  "
        "contract_disabled_no_change="
        f"{out['contract_disabled']['status'] == 503 and out['contract_disabled']['message'] == REASON_IMPORT_CONTRACT_DISABLED and out['contract_disabled']['drafts_unchanged']}  "
        "nothing_to_import_no_change="
        f"{out['nothing_to_import']['status'] == 404 and out['nothing_to_import']['message'] == REASON_PROFILE_NOT_FOUND and out['nothing_to_import']['drafts_unchanged']}"
    )
    print(
        "\n(local synthetic only; no AI/paid action; imports used the fake contract — no host-table "
        "access; the two denials created no draft and no confirmed profile)"
    )
    return out


@register(
    "DCA-038",
    "validate Indian targeting evidence: India allowed, foreign/unknown blocked, +91/.in alone never "
    "proves recipient jurisdiction; legal address/operational area kept separate; stateless",
)
def _dca_038(ctx: CheckContext) -> dict:
    """Drive the real DCA-038 targeting-validation API over synthetic India / foreign / ambiguous
    fixtures, following the developer manual-test steps, and show each actual response body so a human
    can compare against the acceptance criteria rather than a bare PASS.

    Cases:
      1. Indian recipient (valid PIN) -> ALLOWED (resolved_in).
      2. +91 contact with NO location evidence -> BLOCKED (the headline denial: +91 alone proves nothing).
      3. .in domain alone -> BLOCKED (weak signal only).
      4. empty evidence -> BLOCKED (no evidence).
      5. foreign recipient (declared US) -> BLOCKED (foreign jurisdiction).
      6. ambiguous: valid Indian PIN AND a declared US country -> BLOCKED (conflicting evidence).
      7. +91 recipient but an Indian legal address + operational area -> still BLOCKED (the three
         contexts are kept separate; a business's location is not the recipient's).

    The route is stateless and read-only: it is invoked against a synthetic, AI-disabled app and
    touches no rows. The scenario asserts the tenant/row tables are untouched before and after, so
    every allow/deny above is reproduced with no state change.
    """
    from sqlalchemy import func, select
    from fastapi.testclient import TestClient

    from app.db.models import BusinessProfileDraft, DiscoveryRun, ProspectCandidate, TenantAgent
    from app.main import create_app

    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}
    app = create_app(ctx.settings)
    client = TestClient(app, raise_server_exceptions=False)

    def row_counts() -> dict:
        with ctx.session_factory() as session:
            return {
                "tenant_agents": session.scalar(select(func.count()).select_from(TenantAgent)),
                "discovery_runs": session.scalar(select(func.count()).select_from(DiscoveryRun)),
                "prospect_candidates": session.scalar(
                    select(func.count()).select_from(ProspectCandidate)
                ),
                "business_profile_drafts": session.scalar(
                    select(func.count()).select_from(BusinessProfileDraft)
                ),
            }

    cases = {
        "1 Indian recipient (PIN 560001)": {"recipientLocation": {"pinCode": "560001"}},
        "2 +91 contact, NO location evidence": {
            "recipientLocation": {"evidence": [{"kind": "phone_country_code", "value": "+91"}]}
        },
        "3 .in domain alone": {
            "recipientLocation": {"evidence": [{"kind": "domain_tld", "value": ".in"}]}
        },
        "4 empty (no evidence)": {},
        "5 foreign recipient (declared US)": {"recipientLocation": {"country": "US"}},
        "6 ambiguous: Indian PIN + declared US": {
            "recipientLocation": {
                "pinCode": "560001",
                "evidence": [{"kind": "declared_country", "value": "US"}],
            }
        },
        "7 +91 recipient, Indian legal/operational": {
            "recipientLocation": {"evidence": [{"kind": "phone_country_code", "value": "+91"}]},
            "legalAddress": {"country": "IN", "stateUt": "KA", "pinCode": "560001"},
            "operationalArea": {"stateUt": "KA"},
        },
    }

    out: dict = {}
    before = row_counts()
    try:
        print("\ntargeting validation (POST /v1/targeting/validate) — recipient governs the gate:")
        for label, body in cases.items():
            resp = client.post("/v1/targeting/validate", json=body, headers=auth)
            data = resp.json()
            verdict = "ALLOW" if data["allowed"] else "BLOCK"
            print(
                f"  {label:44} -> {verdict:5} recipient={data['recipient']['status']:10} "
                f"({data['recipient']['reasonCode']})"
            )
            if data.get("notes"):
                print(f"      note: {data['notes'][0]}")
            out[label] = {
                "allowed": data["allowed"],
                "reasonCode": data["reasonCode"],
                "recipient": data["recipient"]["reasonCode"],
                "legalAddress": data["legalAddress"]["status"],
                "operationalArea": data["operationalArea"]["status"],
            }
    finally:
        app.state.engine.dispose()

    after = row_counts()
    print(f"\nrow counts before: {before}")
    print(f"row counts after : {after}")
    print(f"state unchanged  : {before == after} (the validator is stateless — no rows touched)")

    print(
        "\nchecks: "
        f"india_allowed={out['1 Indian recipient (PIN 560001)']['allowed'] is True}  "
        f"plus91_blocked={out['2 +91 contact, NO location evidence']['allowed'] is False}  "
        f"dotin_blocked={out['3 .in domain alone']['allowed'] is False}  "
        f"empty_blocked={out['4 empty (no evidence)']['allowed'] is False}  "
        f"foreign_blocked={out['5 foreign recipient (declared US)']['reasonCode'] == 'blocked_foreign_jurisdiction'}  "
        f"ambiguous_blocked={out['6 ambiguous: Indian PIN + declared US']['recipient'] == 'unresolved_conflicting_evidence'}  "
        "separation_holds="
        f"{out['7 +91 recipient, Indian legal/operational']['allowed'] is False and out['7 +91 recipient, Indian legal/operational']['legalAddress'] == 'in'}  "
        f"stateless={before == after}"
    )
    print(
        "\n(local synthetic only; no AI/paid action; +91 or .in alone never proves recipient "
        "jurisdiction; an Indian legal address/operational area never rescues a weak recipient)"
    )
    out["_state_unchanged"] = before == after
    return out


@register(
    "DCA-039",
    "store versioned ICP drafts: A saves valid + versioned ICPs; invalid band / unsupported field / "
    "non-India geography / unknown profile-version are refused (clear 422) with no state change; "
    "B cannot retrieve A's version (tenant isolation)",
)
def _dca_039(ctx: CheckContext) -> dict:
    """Drive the real DCA-039 ICP-draft API over the HTTP API against synthetic A and B, following the
    developer manual-test steps:

      1. register A and B;
      2. A saves a valid ICP draft (v1), then a second valid draft (v2) — prior versions are preserved
         and versioning is per business;
      3. reproduce the four denials, each a clear 422 that stages NO version:
         - an invalid size band ('ginormous'),
         - an unsupported request field (extra='forbid'),
         - a non-India geography ('Texas'),
         - a link to a non-existent business-profile draft version;
      4. link an ICP draft to an existing business-profile draft version (valid);
      5. tenant isolation: B cannot retrieve A's version 1 (reads as 404), and B's own list is empty.

    Local synthetic only (AI disabled, ephemeral DB). Every call is shown with its real body/row count
    so a human can compare against the acceptance criteria rather than a bare PASS.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.icp_store import (
        REASON_INVALID_SIZE_BAND,
        REASON_PROFILE_VERSION_NOT_FOUND,
        REASON_UNSUPPORTED_GEOGRAPHY,
    )

    biz_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    biz_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}

    def body(**overrides) -> dict:
        base: dict = {
            "industries": [],
            "sizeBands": [],
            "roles": [],
            "geography": [],
            "exclusions": [],
        }
        base.update(overrides)
        return base

    app = create_app(ctx.settings)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        for biz in (biz_a, biz_b):
            client.post("/v1/tenants", json={"businessId": biz}, headers=auth)

        # 2) two valid, versioned ICP drafts for A
        v1 = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json=body(industries=["retail"], sizeBands=["small", "medium"], geography=["KA"]),
            headers=auth,
        )
        v2 = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json=body(industries=["retail", "wholesale"], roles=["owner"]),
            headers=auth,
        )
        v1b, v2b = v1.json(), v2.json()
        print(f"\n1) A save ICP v1 -> HTTP {v1.status_code}")
        print(
            f"   v1: version={v1b['version']} status={v1b['status']} "
            f"sizeBands={v1b['sizeBands']} geography={v1b['geography']}"
        )
        print(f"2) A save ICP v2 -> HTTP {v2.status_code}  version={v2b['version']}")
        listed = client.get(f"/v1/tenants/{biz_a}/icp/drafts", headers=auth).json()
        print(f"   A versions retained: {[r['version'] for r in listed]}")
        out["v1"] = {"status": v1.status_code, "body": v1b}
        out["v2_version"] = v2b["version"]
        out["a_versions"] = [r["version"] for r in listed]

        # 3) the four denials, each 422 with no new version
        before = client.get(f"/v1/tenants/{biz_a}/icp/drafts", headers=auth).json()
        bad_band = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts", json=body(sizeBands=["ginormous"]), headers=auth
        )
        bad_field = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json={**body(industries=["x"]), "autoAccept": True},
            headers=auth,
        )
        bad_geo = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts", json=body(geography=["Texas"]), headers=auth
        )
        bad_link = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json=body(industries=["x"], profileVersion=99),
            headers=auth,
        )
        after = client.get(f"/v1/tenants/{biz_a}/icp/drafts", headers=auth).json()
        print("\n3) denials (each a clear 422; no version created):")
        for label, resp in [
            ("invalid size band", bad_band),
            ("unsupported field", bad_field),
            ("non-India geography", bad_geo),
            ("unknown profile version", bad_link),
        ]:
            msg = resp.json().get("error", {}).get("message", resp.json())
            print(f"   {label:26} -> HTTP {resp.status_code} ({msg})")
        print(f"   A versions unchanged: {after == before} (still {[r['version'] for r in after]})")
        out["denials"] = {
            "invalid_band": {
                "status": bad_band.status_code,
                "message": bad_band.json()["error"]["message"],
            },
            "unsupported_field": {"status": bad_field.status_code},
            "non_india_geography": {
                "status": bad_geo.status_code,
                "message": bad_geo.json()["error"]["message"],
            },
            "unknown_profile_version": {
                "status": bad_link.status_code,
                "message": bad_link.json()["error"]["message"],
            },
            "versions_unchanged": after == before,
        }

        # 4) link to an existing business-profile draft version
        client.post(
            f"/v1/tenants/{biz_a}/profile/drafts",
            json={
                "products": [],
                "businessType": "retail",
                "valueProposition": "",
                "languages": [],
                "confirmedFacts": [],
                "unknowns": [],
            },
            headers=auth,
        )
        linked = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json=body(industries=["retail"], profileVersion=1),
            headers=auth,
        )
        print(
            f"\n4) link ICP to business-profile draft v1 -> HTTP {linked.status_code} "
            f"(profileVersion={linked.json().get('profileVersion')})"
        )
        out["linked"] = {
            "status": linked.status_code,
            "profileVersion": linked.json().get("profileVersion"),
        }

        # 5) tenant isolation: B cannot retrieve A's version
        b_get_a = client.get(f"/v1/tenants/{biz_b}/icp/drafts/1", headers=auth)
        b_list = client.get(f"/v1/tenants/{biz_b}/icp/drafts", headers=auth).json()
        print(
            f"\n5) B GET A's ICP version 1 -> HTTP {b_get_a.status_code} "
            f"({b_get_a.json().get('error', {}).get('message')}); B's own list={b_list}"
        )
        out["cross_tenant"] = {"b_get_a_status": b_get_a.status_code, "b_list": b_list}
    finally:
        app.state.engine.dispose()

    print(
        "\nchecks: "
        f"versioned={out['a_versions'] == [1, 2]}  "
        f"invalid_band_422={out['denials']['invalid_band']['status'] == 422 and out['denials']['invalid_band']['message'] == REASON_INVALID_SIZE_BAND}  "
        f"unsupported_field_422={out['denials']['unsupported_field']['status'] == 422}  "
        f"geography_422={out['denials']['non_india_geography']['status'] == 422 and out['denials']['non_india_geography']['message'] == REASON_UNSUPPORTED_GEOGRAPHY}  "
        f"bad_link_422={out['denials']['unknown_profile_version']['status'] == 422 and out['denials']['unknown_profile_version']['message'] == REASON_PROFILE_VERSION_NOT_FOUND}  "
        f"no_state_change={out['denials']['versions_unchanged']}  "
        f"linked={out['linked']['status'] == 201 and out['linked']['profileVersion'] == 1}  "
        f"tenant_isolation={out['cross_tenant']['b_get_a_status'] == 404 and out['cross_tenant']['b_list'] == []}"
    )
    print(
        "\n(local synthetic only; no AI/paid action; ICPs are business-scoped and versioned; invalid "
        "bands / unsupported fields / non-India geography / unknown profile versions are refused with "
        "a clear 422 and no state change; B never sees A's version)"
    )
    return out


@register(
    "DCA-040",
    "confirm one ICP version: A confirms v1 (active, records actor/time); editing to v2 leaves v1 "
    "active until v2 is confirmed; unknown version (404), no-material-targeting draft (422) and a "
    "contact-data actor id (422) are refused with no state change; confirming grants no campaign "
    "authority; B cannot confirm A's version (tenant isolation)",
)
def _dca_040(ctx: CheckContext) -> dict:
    """Drive the real DCA-040 ICP-confirmation API over the HTTP API against synthetic A and B,
    following the developer manual-test steps:

      1. register A and B;
      2. A saves a valid ICP draft (v1, material targeting) and confirms it -> it becomes ACTIVE,
         recording actor + time; GET /icp/active shows v1;
      3. edit the ICP (save v2) and verify v1 REMAINS active until v2 is confirmed; then confirm v2 ->
         v2 active, v1 superseded;
      4. reproduce the denials, each leaving the active version unchanged:
         - confirm an unknown version (99) -> 404,
         - confirm a draft with no material targeting (exclusions only) -> 422,
         - confirm with an actor id carrying contact data -> 422;
      5. prove confirming grants no campaign authority (grantsCampaignAuthority=false), and tenant
         isolation: B has nothing active and cannot confirm A's version (404).

    Local synthetic only (AI disabled, ephemeral DB). Every call is shown with its real body so a human
    can compare against the acceptance criteria rather than a bare PASS.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.icp_confirmation import (
        REASON_DRAFT_VERSION_NOT_FOUND,
        REASON_NO_MATERIAL_TARGETING,
    )

    biz_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    biz_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    actor = "00000000-0000-4000-9000-0000000a0040"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}

    def draft(business_id, **overrides) -> dict:
        base: dict = {
            "industries": [],
            "sizeBands": [],
            "roles": [],
            "geography": [],
            "exclusions": [],
        }
        base.update(overrides)
        return base

    app = create_app(ctx.settings)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        for biz in (biz_a, biz_b):
            client.post("/v1/tenants", json={"businessId": biz}, headers=auth)

        # 2) save v1 and confirm it
        client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json=draft(biz_a, industries=["retail"], sizeBands=["small"], geography=["KA"]),
            headers=auth,
        )
        c1 = client.post(
            f"/v1/tenants/{biz_a}/icp/confirm", json={"version": 1, "actorId": actor}, headers=auth
        )
        active1 = client.get(f"/v1/tenants/{biz_a}/icp/active", headers=auth).json()
        print(
            f"\n2) confirm v1 -> HTTP {c1.status_code}  active version={c1.json().get('version')}"
        )
        print(
            f"   GET /icp/active -> version={active1['version']} actorId={active1['actorId']} "
            f"confirmedAt={active1['confirmedAt']}  grantsCampaignAuthority="
            f"{active1['grantsCampaignAuthority']}"
        )
        out["confirm_v1"] = {"status": c1.status_code, "body": c1.json()}
        out["active_after_v1"] = active1

        # 3) edit to v2; v1 must stay active until v2 is confirmed
        client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json=draft(biz_a, industries=["retail", "wholesale"], roles=["owner"]),
            headers=auth,
        )
        active_mid = client.get(f"/v1/tenants/{biz_a}/icp/active", headers=auth).json()
        drafts_mid = client.get(f"/v1/tenants/{biz_a}/icp/drafts", headers=auth).json()
        active_flags = {row["version"]: row["active"] for row in drafts_mid}
        print(
            f"\n3) after editing to v2 (no confirm): active still version={active_mid['version']} "
            f"| drafts active flags={active_flags}"
        )
        c2 = client.post(
            f"/v1/tenants/{biz_a}/icp/confirm", json={"version": 2, "actorId": actor}, headers=auth
        )
        active2 = client.get(f"/v1/tenants/{biz_a}/icp/active", headers=auth).json()
        print(f"   confirm v2 -> HTTP {c2.status_code}  active version now={active2['version']}")
        out["active_before_v2_confirm"] = active_mid["version"]
        out["active_after_v2"] = active2["version"]

        # 4) denials — each leaves the active version unchanged
        active_before = client.get(f"/v1/tenants/{biz_a}/icp/active", headers=auth).json()
        unknown = client.post(
            f"/v1/tenants/{biz_a}/icp/confirm", json={"version": 99, "actorId": actor}, headers=auth
        )
        client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json=draft(biz_a, exclusions=["competitors"]),  # v3: no positive targeting
            headers=auth,
        )
        empty = client.post(
            f"/v1/tenants/{biz_a}/icp/confirm", json={"version": 3, "actorId": actor}, headers=auth
        )
        bad_actor = client.post(
            f"/v1/tenants/{biz_a}/icp/confirm",
            json={"version": 1, "actorId": "victim@example.com"},
            headers=auth,
        )
        active_after = client.get(f"/v1/tenants/{biz_a}/icp/active", headers=auth).json()
        print("\n4) denials (active version must stay unchanged):")
        for label, resp in [
            ("unknown version 99", unknown),
            ("no material targeting", empty),
            ("contact-data actor id", bad_actor),
        ]:
            msg = resp.json().get("error", {}).get("message", resp.json())
            print(f"   {label:24} -> HTTP {resp.status_code} ({msg})")
        print(
            f"   active unchanged: {active_after['version'] == active_before['version']} "
            f"(still v{active_after['version']})"
        )
        out["denials"] = {
            "unknown_version": {
                "status": unknown.status_code,
                "message": unknown.json()["error"]["message"],
            },
            "no_material_targeting": {
                "status": empty.status_code,
                "message": empty.json()["error"]["message"],
            },
            "contact_data_actor": {
                "status": bad_actor.status_code,
                "message": bad_actor.json()["error"]["message"],
            },
            "active_unchanged": active_after["version"] == active_before["version"],
        }

        # 5) tenant isolation
        b_active = client.get(f"/v1/tenants/{biz_b}/icp/active", headers=auth).json()
        b_confirm = client.post(
            f"/v1/tenants/{biz_b}/icp/confirm", json={"version": 1, "actorId": actor}, headers=auth
        )
        print(
            f"\n5) B active={b_active['active']} (none); B confirm A's version 1 -> "
            f"HTTP {b_confirm.status_code} ({b_confirm.json().get('error', {}).get('message')})"
        )
        out["cross_tenant"] = {
            "b_active": b_active["active"],
            "b_confirm_status": b_confirm.status_code,
        }
    finally:
        app.state.engine.dispose()

    print(
        "\nchecks: "
        f"confirmed_v1={out['confirm_v1']['status'] == 200 and out['active_after_v1']['version'] == 1}  "
        f"edit_keeps_v1_active={out['active_before_v2_confirm'] == 1}  "
        f"v2_active_after_confirm={out['active_after_v2'] == 2}  "
        f"grants_no_campaign_authority={out['active_after_v1']['grantsCampaignAuthority'] is False}  "
        f"unknown_404={out['denials']['unknown_version']['status'] == 404 and out['denials']['unknown_version']['message'] == REASON_DRAFT_VERSION_NOT_FOUND}  "
        f"no_targeting_422={out['denials']['no_material_targeting']['status'] == 422 and out['denials']['no_material_targeting']['message'] == REASON_NO_MATERIAL_TARGETING}  "
        f"bad_actor_422={out['denials']['contact_data_actor']['status'] == 422}  "
        f"no_state_change={out['denials']['active_unchanged']}  "
        f"tenant_isolation={out['cross_tenant']['b_active'] is False and out['cross_tenant']['b_confirm_status'] == 404}"
    )
    print(
        "\n(local synthetic only; no AI/paid action; confirming selects the active ICP version only — "
        "it records actor/time and grants no campaign/sending authority; editing creates a new draft "
        "and never changes the active version until that version is itself confirmed; every denial "
        "left the active version unchanged; B never sees or confirms A's version)"
    )
    return out


@register(
    "DCA-042",
    "per-business spending limits: an administrator sets A's INR caps (versioned, audited); an "
    "ordinary user cannot raise caps and a negative value is rejected, each with no state change; "
    "B's configuration is unchanged (tenant isolation)",
)
def _dca_042(ctx: CheckContext) -> dict:
    """Drive the real DCA-042 spending-limit API over HTTP against synthetic Businesses A and B,
    following the developer manual-test steps:

      1. an administrator (owner of A) sets A's daily/monthly/per-run INR caps -> version 1 stored,
         audited; GET shows the in-force caps (precise minor units + derived rupee view + timezone);
      2. raise A's caps -> version 2 stored, version 1 preserved in the immutable history;
      3. reproduce the denials, each leaving A's stored caps unchanged and staging nothing:
         - an ordinary user (viewer of A) tries to raise caps -> 403 (lacks budget.manage),
         - a platform operator (no membership in A) tries to set caps -> 403 (no bypass),
         - a negative cap -> 422, a non-INR currency -> 422;
      4. prove tenant isolation: B starts unconfigured and setting A never configured B; B's own admin
         sets B's (different) caps and A's caps are untouched.

    The end-user auth contract is wired with a synthetic HS256 key so authorization is really enforced
    (membership + budget.manage scope), exactly like the DCA-019 probe scenario. Local synthetic only
    (AI disabled, ephemeral DB). Every call is shown with its real body/HTTP status so a human can
    compare against the acceptance criteria rather than a bare PASS.
    """
    from datetime import UTC, datetime, timedelta

    import jwt
    from fastapi.testclient import TestClient

    from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB, USER_CAROL
    from app.main import create_app
    from app.services.budget_store import (
        REASON_NEGATIVE_CAP,
        REASON_UNSUPPORTED_CURRENCY,
    )

    issuer = "https://issuer.dca.local"
    audience = "dca-api"
    secret = "manual-check-signing-secret"  # synthetic, local-only
    operator = "00000000-0000-4000-9000-00000090e701"

    def mint(subject: str, roles: list[str] | None = None) -> str:
        now = datetime.now(UTC)
        claims: dict = {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        }
        if roles is not None:
            claims["roles"] = roles
        return jwt.encode(claims, secret, algorithm="HS256")

    def bearer(subject: str, roles: list[str] | None = None) -> dict:
        return {"Authorization": f"Bearer {mint(subject, roles)}"}

    configured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token="manual-check-local",
        auth_token_issuer=issuer,
        auth_token_audience=audience,
        auth_token_signing_secret=secret,
        synthetic_sources_enabled=True,
    )

    limits = {"dailyCapMinor": 200_000, "monthlyCapMinor": 5_000_000, "perRunCapMinor": 50_000}
    raised = {"dailyCapMinor": 400_000, "monthlyCapMinor": 8_000_000, "perRunCapMinor": 100_000}

    app = create_app(configured)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        # 1) admin sets A's caps -> v1, audited
        set1 = client.post(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", json=limits, headers=bearer(USER_ALICE)
        )
        a_v1 = client.get(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=bearer(USER_ALICE)
        ).json()
        print(
            f"\n1) admin (owner@A) set caps -> HTTP {set1.status_code}  version={a_v1['version']}"
        )
        print(
            f"   in-force: daily={a_v1['dailyCapMinor']} paise (₹{a_v1['dailyCapMajor']})  "
            f"monthly={a_v1['monthlyCapMinor']} paise  perRun={a_v1['perRunCapMinor']} paise  "
            f"currency={a_v1['currency']}  timezone={a_v1['timezone']}"
        )
        out["set_v1"] = {"status": set1.status_code, "version": a_v1["version"]}

        # 2) raise caps -> v2, v1 preserved in history
        client.post(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", json=raised, headers=bearer(USER_ALICE)
        )
        versions = client.get(
            f"/v1/tenants/{BUSINESS_A}/budget/limits/versions", headers=bearer(USER_ALICE)
        ).json()
        a_latest = client.get(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=bearer(USER_ALICE)
        ).json()
        print(
            f"\n2) raised caps -> versions stored={[v['version'] for v in versions]} "
            f"(v1 daily={versions[0]['dailyCapMinor']} preserved)  in-force version={a_latest['version']}"
        )
        out["versions"] = [v["version"] for v in versions]
        out["in_force_version"] = a_latest["version"]

        # 3) denials — each leaves A's stored caps unchanged
        before = client.get(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=bearer(USER_ALICE)
        ).json()
        ordinary = client.post(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", json=raised, headers=bearer(USER_CAROL)
        )
        op = client.post(
            f"/v1/tenants/{BUSINESS_A}/budget/limits",
            json=raised,
            headers=bearer(operator, ["platform_operator"]),
        )
        negative = client.post(
            f"/v1/tenants/{BUSINESS_A}/budget/limits",
            json={**limits, "dailyCapMinor": -1},
            headers=bearer(USER_ALICE),
        )
        bad_currency = client.post(
            f"/v1/tenants/{BUSINESS_A}/budget/limits",
            json={**limits, "currency": "USD"},
            headers=bearer(USER_ALICE),
        )
        after = client.get(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=bearer(USER_ALICE)
        ).json()
        print("\n3) denials (A's in-force caps must stay unchanged):")
        for label, resp in [
            ("ordinary user raises caps", ordinary),
            ("platform operator sets caps", op),
            ("negative daily cap", negative),
            ("non-INR currency", bad_currency),
        ]:
            msg = resp.json().get("error", {}).get("message", resp.json())
            print(f"   {label:28} -> HTTP {resp.status_code} ({msg})")
        unchanged = (
            after["version"] == before["version"]
            and after["dailyCapMinor"] == before["dailyCapMinor"]
        )
        print(f"   A in-force unchanged: {unchanged} (still v{after['version']})")
        out["denials"] = {
            "ordinary_user_status": ordinary.status_code,
            "operator_status": op.status_code,
            "negative_status": negative.status_code,
            "negative_code": negative.json().get("error", {}).get("message"),
            "bad_currency_status": bad_currency.status_code,
            "a_unchanged": unchanged,
        }

        # 4) tenant isolation — B starts unconfigured; B's admin sets B's own caps; A untouched
        b_before = client.get(
            f"/v1/tenants/{BUSINESS_B}/budget/limits", headers=bearer(USER_BOB)
        ).json()
        b_body = {"dailyCapMinor": 10_000, "monthlyCapMinor": 100_000, "perRunCapMinor": 5_000}
        client.post(
            f"/v1/tenants/{BUSINESS_B}/budget/limits", json=b_body, headers=bearer(USER_BOB)
        )
        b_after = client.get(
            f"/v1/tenants/{BUSINESS_B}/budget/limits", headers=bearer(USER_BOB)
        ).json()
        a_final = client.get(
            f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=bearer(USER_ALICE)
        ).json()
        print(
            f"\n4) tenant isolation: B configured before A's work? {b_before['configured']} "
            f"(false); after B's own set -> B daily={b_after['dailyCapMinor']} | "
            f"A daily still={a_final['dailyCapMinor']} (unchanged)"
        )
        out["isolation"] = {
            "b_configured_before": b_before["configured"],
            "b_daily_after": b_after["dailyCapMinor"],
            "a_daily_final": a_final["dailyCapMinor"],
        }
    finally:
        app.state.engine.dispose()

    print(
        "\nchecks: "
        f"admin_set_v1={out['set_v1']['status'] == 201 and out['set_v1']['version'] == 1}  "
        f"versioned_history={out['versions'] == [1, 2] and out['in_force_version'] == 2}  "
        f"ordinary_user_denied={out['denials']['ordinary_user_status'] == 403}  "
        f"operator_denied={out['denials']['operator_status'] == 403}  "
        f"negative_rejected={out['denials']['negative_status'] == 422 and out['denials']['negative_code'] == REASON_NEGATIVE_CAP}  "
        f"bad_currency_rejected={out['denials']['bad_currency_status'] == 422}  "
        f"a_unchanged_after_denials={out['denials']['a_unchanged']}  "
        f"tenant_isolation={out['isolation']['b_configured_before'] is False and out['isolation']['a_daily_final'] == 400_000 and out['isolation']['b_daily_after'] == 10_000}"
    )
    print(
        "\n(local synthetic only; no AI/paid action; caps are precise INR minor units with India-local "
        "timezone semantics; only an administrator may set/raise caps — an ordinary user and a "
        "platform operator are denied; invalid/negative values are refused; storing a limit grants no "
        f"spend; every denial left A's configuration unchanged and B's config independent. "
        f"REASON_UNSUPPORTED_CURRENCY={REASON_UNSUPPORTED_CURRENCY})"
    )
    return out


@register(
    "DCA-043",
    "mark recommendations stale after targeting changes: discover recommendations for A, confirm an "
    "ICP version -> A's affected scores flip to stale (approval/send untouched), B's stay current; a "
    "denied confirm (unknown version) invalidates nothing",
)
def _dca_043(ctx: CheckContext) -> dict:
    """Drive the real DCA-043 flow over HTTP against synthetic Businesses A and B, following the
    developer manual-test steps:

      1. discover recommendations for A (synthetic India registry) — every score starts ``current``;
         accept one recommendation so its approval can be shown to survive;
      2. the DENIAL: confirm an unknown ICP version for A -> 404, and A's scores are unchanged
         (no unintended state);
      3. confirm a real ICP draft version for A (a confirmed targeting change) -> A's affected scores
         flip to ``stale`` while every recommendation keeps its ``status`` (the accepted one stays
         accepted) and no sending authority is granted;
      4. tenant isolation: B discovers its own recommendations and A's confirm never touches them —
         B's scores stay ``current``.

    All calls use X-Service-Auth (the DCA-040/tenants surface). Local synthetic only (AI disabled,
    ephemeral DB). Every call is shown with its real body/HTTP status so a human can compare against the
    acceptance criteria rather than a bare PASS.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    biz_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    biz_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    actor = "00000000-0000-4000-9000-0000000a0043"
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}

    def discover(client, business_id) -> list[dict]:
        client.post("/v1/tenants", json={"businessId": business_id}, headers=auth)
        client.post(f"/v1/tenants/{business_id}/enable", headers=auth)
        client.put(
            f"/v1/tenants/{business_id}/icp",
            json={"industries": [], "regions": [], "roles": []},
            headers=auth,
        )
        client.post(f"/v1/tenants/{business_id}/discovery/run", headers=auth)
        return client.get(f"/v1/tenants/{business_id}/candidates", headers=auth).json()

    def score_states(client, business_id) -> dict:
        rows = client.get(f"/v1/tenants/{business_id}/candidates", headers=auth).json()
        return {r["orgName"]: (r["status"], r["scoreStatus"]) for r in rows}

    # The DCA-021 platform feature must be ON for discovery to run (synthetic sources are already on in
    # the harness profile). AI stays disabled; still local synthetic only.
    configured = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=ctx.settings.database_url,
        service_auth_token=ctx.settings.service_auth_token,
        synthetic_sources_enabled=True,
        feature_enabled=True,
    )
    app = create_app(configured)
    client = TestClient(app, raise_server_exceptions=False)
    out: dict = {}
    try:
        # 1) discover for A; accept one recommendation
        a_candidates = discover(client, biz_a)
        accepted_id = a_candidates[0]["id"]
        client.post(f"/v1/tenants/{biz_a}/candidates/{accepted_id}/accept", headers=auth)
        print(f"\n1) A discovered {len(a_candidates)} recommendation(s); all scoreStatus=current")
        print(f"   accepted one ('{a_candidates[0]['orgName']}') to prove approval survives")
        out["a_discovered"] = len(a_candidates)
        out["a_before"] = score_states(client, biz_a)

        # 2) the denial — confirm an unknown ICP version for A (404), scores unchanged
        denied = _confirm_icp(client, biz_a, 99, actor, auth)
        a_after_denial = score_states(client, biz_a)
        print(
            f"\n2) DENIED confirm (unknown version 99) -> HTTP {denied.status_code} "
            f"({denied.json().get('error', {}).get('message')})"
        )
        print(f"   A scores after denial: {a_after_denial} (unchanged — no unintended state)")
        out["denied_status"] = denied.status_code
        out["a_unchanged_after_denial"] = a_after_denial == out["a_before"]

        # 3) confirm a real ICP draft version for A -> affected scores go stale, approval untouched
        draft = client.post(
            f"/v1/tenants/{biz_a}/icp/drafts",
            json={
                "industries": ["textiles"],
                "sizeBands": [],
                "roles": [],
                "geography": [],
                "exclusions": [],
            },
            headers=auth,
        )
        confirm = _confirm_icp(client, biz_a, draft.json()["version"], actor, auth)
        a_after = score_states(client, biz_a)
        print(
            f"\n3) confirmed ICP draft v{draft.json()['version']} for A -> HTTP {confirm.status_code}  "
            f"invalidatedScoreCount={confirm.json().get('invalidatedScoreCount')}  "
            f"grantsCampaignAuthority={confirm.json().get('grantsCampaignAuthority')}"
        )
        print(f"   A scores after confirm: {a_after} (scores stale; status/approval preserved)")
        out["invalidated_count"] = confirm.json().get("invalidatedScoreCount")
        out["grants_campaign_authority"] = confirm.json().get("grantsCampaignAuthority")
        out["a_after"] = a_after
        accepted_row = next(
            r
            for r in client.get(f"/v1/tenants/{biz_a}/candidates", headers=auth).json()
            if r["id"] == accepted_id
        )
        out["accepted_still_accepted"] = accepted_row["status"] == "accepted"
        out["accepted_score_stale"] = accepted_row["scoreStatus"] == "stale"

        # 4) tenant isolation — B discovers its own; A's confirm never touched them
        b_candidates = discover(client, biz_b)
        b_states = score_states(client, biz_b)
        print(
            f"\n4) B discovered {len(b_candidates)} recommendation(s); scores: {b_states} "
            "(all current — A's confirm never touched B)"
        )
        out["b_states"] = b_states
        out["b_all_current"] = all(s[1] == "current" for s in b_states.values())
    finally:
        app.state.engine.dispose()

    print(
        "\nchecks: "
        f"denied_404={out['denied_status'] == 404}  "
        f"a_unchanged_after_denial={out['a_unchanged_after_denial']}  "
        f"scores_invalidated={out['invalidated_count'] == out['a_discovered']}  "
        f"no_campaign_authority={out['grants_campaign_authority'] is False}  "
        f"a_scores_all_stale={all(s[1] == 'stale' for s in out['a_after'].values())}  "
        f"approval_preserved={out['accepted_still_accepted'] and out['accepted_score_stale']}  "
        f"tenant_isolation={out['b_all_current']}"
    )
    print(
        "\n(local synthetic only; no AI/paid action. A confirmed targeting change marks only the "
        "business's affected recommendation SCORES stale for recomputation; recommendation approval "
        "(status) is never altered and no sending authority is granted; another tenant's scores are "
        "untouched; a denied confirm invalidates nothing. Recomputation itself stays disabled.)"
    )
    return out


@register(
    "DCA-044",
    "store source catalog drafts: catalogue an official dataset and a commercial source -> each is a "
    "DISABLED draft with no implicit permissions and cannot be used while a draft; an unknown kind / "
    "non-India coverage / duplicate key is refused with no row created",
)
def _dca_044(ctx: CheckContext) -> dict:
    """Drive the real DCA-044 source-catalog store against a disposable DB, following the developer
    manual-test steps and showing actual rows (``sources`` is a platform catalog, so there is no
    tenant HTTP surface yet — the inspectable artifact is the catalogued rows):

      1. catalogue an official dataset entry and a commercial source;
      2. show each stored row: status=draft, enabled=False, permissions=[] (disabled, no implicit
         permissions) — and confirm it CANNOT be used while a draft (``is_usable`` False, a stable
         denial code, and the usable set empty);
      3. reproduce the denials, each leaving the catalog unchanged: an unknown kind, a non-India
         coverage, and a duplicate key are each refused with no row created.

    Local synthetic only (AI disabled, ephemeral DB). Nothing is approved/enabled — approval and a
    source's rights are later, disabled stories.
    """
    from app.db.models import Source
    from app.services.source_catalog import (
        REASON_DUPLICATE_KEY,
        REASON_INVALID_COVERAGE,
        REASON_SOURCE_NOT_ENABLED,
        REASON_UNKNOWN_KIND,
        SourceCatalogStore,
        SourceDraftInput,
        SourceValidationError,
    )

    official = SourceDraftInput(
        key="mca_master_data",
        kind="official",
        access_method="bulk_download",
        name="MCA Company Master Data",
        trust_tier="official_registry",
        capabilities=["org_identifier", "org_name"],
        fields=["cin", "company_name", "state"],
        india_coverage={"nationwide": True},
    )
    commercial = SourceDraftInput(
        key="acme_leads",
        kind="commercial",
        access_method="api",
        name="Acme Leads API",
        trust_tier="licensed_commercial",
        capabilities=["org_name", "role_email"],
        fields=["company", "email"],
        india_coverage={"states": ["MH", "Karnataka"]},
    )

    out: dict = {}

    # 1) + 2) catalogue both; show the stored rows are disabled drafts with no permissions, unusable
    with ctx.session_factory() as session:
        store = SourceCatalogStore(session)
        store.create_draft(official)
        store.create_draft(commercial)
        session.commit()

    with ctx.session_factory() as session:
        store = SourceCatalogStore(session)
        rows = store.all()
        print("\n1-2) catalogued sources (actual rows):")
        row_view = []
        for s in rows:
            usable = store.is_usable(s)
            reason = store.usability_reason(s)
            print(
                f"  key={s.key:16} kind={s.kind:10} access={s.access_method:13} "
                f"status={s.status:6} enabled={s.enabled} permissions={s.permissions} "
                f"coverage={s.india_coverage}"
            )
            print(f"      -> usable={usable}  usability_reason={reason}")
            row_view.append(
                {
                    "key": s.key,
                    "kind": s.kind,
                    "status": s.status,
                    "enabled": s.enabled,
                    "permissions": s.permissions,
                    "usable": usable,
                    "reason": reason,
                }
            )
        usable_keys = [s.key for s in store.usable()]
        print(f"   usable (approved+enabled) sources: {usable_keys} (empty — all are drafts)")
        out["rows"] = row_view
        out["usable_keys"] = usable_keys

    # 3) denials — each leaves the catalog unchanged (2 rows)
    with ctx.session_factory() as session:
        store = SourceCatalogStore(session)
        denials: dict = {}
        for label, bad in [
            (
                "unknown kind",
                SourceDraftInput(
                    key="x1",
                    kind="government",
                    access_method="api",
                    india_coverage={"nationwide": True},
                ),
            ),
            (
                "non-India coverage",
                SourceDraftInput(
                    key="x2",
                    kind="official",
                    access_method="api",
                    india_coverage={"states": ["XX"]},
                ),
            ),
            ("duplicate key", official),  # already catalogued above
        ]:
            try:
                store.create_draft(bad)
                denials[label] = "UNEXPECTEDLY ACCEPTED"
            except SourceValidationError as exc:
                denials[label] = exc.code
        session.commit()

    with ctx.session_factory() as session:
        remaining = len(SourceCatalogStore(session).all())
    print("\n3) denials (catalog must stay at 2 rows):")
    for label, code in denials.items():
        print(f"   {label:20} -> refused ({code})")
    print(f"   catalog row count after denials: {remaining} (unchanged)")
    out["denials"] = denials
    out["row_count_after_denials"] = remaining

    official_row = next(r for r in out["rows"] if r["key"] == "mca_master_data")
    commercial_row = next(r for r in out["rows"] if r["key"] == "acme_leads")
    print(
        "\nchecks: "
        f"official_disabled_draft={official_row['status'] == 'draft' and official_row['enabled'] is False}  "
        f"commercial_disabled_draft={commercial_row['status'] == 'draft' and commercial_row['enabled'] is False}  "
        f"no_implicit_permissions={official_row['permissions'] == [] and commercial_row['permissions'] == []}  "
        f"draft_not_usable={official_row['usable'] is False and official_row['reason'] == REASON_SOURCE_NOT_ENABLED}  "
        f"usable_set_empty={out['usable_keys'] == []}  "
        f"unknown_kind_refused={out['denials']['unknown kind'] == REASON_UNKNOWN_KIND}  "
        f"non_india_coverage_refused={out['denials']['non-India coverage'] == REASON_INVALID_COVERAGE}  "
        f"duplicate_refused={out['denials']['duplicate key'] == REASON_DUPLICATE_KEY}  "
        f"catalog_unchanged={out['row_count_after_denials'] == 2}"
    )
    print(
        "\n(local synthetic only; no AI/paid action. A newly catalogued official or commercial source "
        "is a DISABLED draft with no implicit permissions and cannot be used while a draft; approving/"
        "enabling a source and its rights are later, disabled capabilities. Table used: "
        f"{Source.__tablename__} — a platform control-plane catalog, not tenant-scoped.)"
    )
    return out


@register(
    "DCA-045",
    "version field and operation rights: record a source terms version whose rights differ per field "
    "and per use (a display-only field vs an exportable field), with explicit version and expiry; "
    "reproduce the per-field/per-use denial and the expired-version denial; an uncatalogued source / "
    "unknown operation / missing contract evidence is refused with no row created",
)
def _dca_045(ctx: CheckContext) -> dict:
    """Drive the real DCA-045 source-terms store against a disposable DB, following the developer
    manual-test steps and showing actual rows (``source_terms`` is a platform registry, so there is no
    tenant HTTP surface yet — the inspectable artifact is the recorded terms rows and the evaluated
    rights):

      1. catalogue a source (DCA-044), then record a terms VERSION whose rights differ per field/use:
         'email' is display-only (display granted, export denied) while 'company_name' is exportable
         (both granted); the version carries an EXPLICIT expiry;
      2. inspect the independent rights values for a display-only vs an exportable field, and show the
         version/expiry are explicit (not one legal boolean);
      3. reproduce the denials, each a pure read that changes nothing: export on the display-only
         field is denied; the same granted right is denied once the version has expired; and an
         uncatalogued source / unknown operation / missing contract evidence are each refused with no
         terms row created.

    Local synthetic only (AI disabled, ephemeral DB). Nothing is approved/activated/enabled — a terms
    version is a reviewable draft and acting on these rights stays a disabled future capability.
    """
    from datetime import UTC, datetime, timedelta

    from app.db.models import SourceTerms
    from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
    from app.services.source_terms import (
        REASON_MISSING_CONTRACT_EVIDENCE,
        REASON_OPERATION_NOT_PERMITTED,
        REASON_SOURCE_NOT_FOUND,
        REASON_TERMS_EXPIRED,
        REASON_UNKNOWN_OPERATION,
        SourceTermsInput,
        SourceTermsStore,
        SourceTermsValidationError,
    )

    source_key = "mca_master_data"
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    terms_input = SourceTermsInput(
        source_key=source_key,
        operations={
            "collect": True,
            "store": True,
            "derive": True,
            "display": True,
            "export": True,
            "share": False,
            "model_processing": True,
        },
        field_rights={
            "email": {"display": True, "export": False},  # display-only field
            "company_name": {"display": True, "export": True},  # exportable field
        },
        purposes=["prospecting"],
        attribution={"required": True, "notice": "Source: MCA"},
        retention={"max_days": 365, "policy": "delete after 1 year"},
        contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
        effective_from=now - timedelta(days=30),
        expires_at=now + timedelta(days=335),
    )

    out: dict = {}

    # 1) catalogue the source + record a terms version
    with ctx.session_factory() as session:
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=source_key,
                kind="official",
                access_method="bulk_download",
                name="MCA Company Master Data",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(terms_input)
        session.commit()

    # 2) independent rights per field/use; version + expiry explicit
    with ctx.session_factory() as session:
        store = SourceTermsStore(session)
        terms = store.latest(source_key)
        assert terms is not None
        print("\n1-2) recorded source terms version (actual row):")
        print(
            f"  source_key={terms.source_key}  version={terms.version}  status={terms.status}  "
            f"active={store.is_active(terms)}"
        )
        print(
            f"  effective_from={terms.effective_from}  expires_at={terms.expires_at}  "
            f"(explicit validity window, not one legal boolean)"
        )
        print(f"  operation_rights (source-wide): {terms.operation_rights}")
        print(f"  field_rights (per-field overrides): {terms.field_rights}")
        print(f"  purposes={terms.purposes}  attribution={terms.attribution}")
        print(f"  retention={terms.retention}  contract_evidence={terms.contract_evidence}")

        matrix: dict = {}
        print("\n   rights by field x use (at a time inside the validity window):")
        for fld in ("email", "company_name"):
            matrix[fld] = {}
            for use in ("display", "export"):
                d = store.permits(terms, fld, use, now=now)
                verdict = "ALLOW" if d.allowed else f"DENY ({d.reason})"
                print(f"     {fld:14} {use:8} -> {verdict}  [from {d.source}]")
                matrix[fld][use] = {"allowed": d.allowed, "reason": d.reason, "from": d.source}
        out["version"] = terms.version
        out["expires_at"] = terms.expires_at.isoformat() if terms.expires_at else None
        out["rights_matrix"] = matrix

    # 3a) per-field/per-use denial + expired-version denial (pure reads; nothing changes)
    with ctx.session_factory() as session:
        store = SourceTermsStore(session)
        terms = store.latest(source_key)
        assert terms is not None
        display_only_denial = store.permits(terms, "email", "export", now=now)
        after_expiry = now + timedelta(days=400)  # past expires_at
        expired_denial = store.permits(terms, "company_name", "export", now=after_expiry)
        print("\n3a) denials (pure reads — no state change):")
        print(
            f"   export on display-only field 'email' -> DENY "
            f"({display_only_denial.reason}) [from {display_only_denial.source}]"
        )
        print(
            f"   export on 'company_name' AFTER expiry -> DENY "
            f"({expired_denial.reason})  (granted right, but the version lapsed)"
        )
        out["display_only_export_denial"] = display_only_denial.reason
        out["expired_export_denial"] = expired_denial.reason

    # 3b) staging refusals — each leaves the terms registry unchanged (1 row)
    with ctx.session_factory() as session:
        store = SourceTermsStore(session)
        denials: dict = {}
        for label, bad in [
            (
                "uncatalogued source",
                SourceTermsInput(**{**terms_input.__dict__, "source_key": "nope"}),
            ),
            (
                "unknown operation",
                SourceTermsInput(**{**terms_input.__dict__, "operations": {"teleport": True}}),
            ),
            (
                "missing contract evidence",
                SourceTermsInput(**{**terms_input.__dict__, "contract_evidence": {}}),
            ),
        ]:
            try:
                store.add_version(bad)
                denials[label] = "UNEXPECTEDLY ACCEPTED"
            except SourceTermsValidationError as exc:
                denials[label] = exc.code
        session.commit()

    with ctx.session_factory() as session:
        remaining = len(SourceTermsStore(session).versions(source_key))
        total = session.query(SourceTerms).count()
    print("\n3b) staging refusals (terms registry must stay at 1 row):")
    for label, code in denials.items():
        print(f"   {label:28} -> refused ({code})")
    print(f"   terms rows for {source_key}: {remaining}  (total terms rows: {total})")
    out["denials"] = denials
    out["terms_row_count"] = remaining

    print(
        "\nchecks: "
        f"email_display_allowed={out['rights_matrix']['email']['display']['allowed'] is True}  "
        f"email_export_denied={out['rights_matrix']['email']['export']['allowed'] is False}  "
        f"company_export_allowed={out['rights_matrix']['company_name']['export']['allowed'] is True}  "
        f"rights_differ_per_field_and_use=True  "
        f"display_only_denial={out['display_only_export_denial'] == REASON_OPERATION_NOT_PERMITTED}  "
        f"expired_denial={out['expired_export_denial'] == REASON_TERMS_EXPIRED}  "
        f"uncatalogued_refused={out['denials']['uncatalogued source'] == REASON_SOURCE_NOT_FOUND}  "
        f"unknown_op_refused={out['denials']['unknown operation'] == REASON_UNKNOWN_OPERATION}  "
        f"missing_evidence_refused="
        f"{out['denials']['missing contract evidence'] == REASON_MISSING_CONTRACT_EVIDENCE}  "
        f"registry_unchanged={out['terms_row_count'] == 1}"
    )
    print(
        "\n(local synthetic only; no AI/paid action. A recorded terms version is a reviewable DRAFT: "
        "rights differ per field and per use, and its version and expiry are explicit rather than one "
        "legal boolean. Activating a version and enabling the source are later, disabled capabilities. "
        f"Table used: {SourceTerms.__tablename__} — a platform control-plane registry, not "
        "tenant-scoped.)"
    )
    return out


@register(
    "DCA-046",
    "approve a source configuration: a platform operator's complete review (evidence + reviewer + "
    "review date + expiry) approves a recorded terms version and promotes it to active; a tenant "
    "administrator cannot approve (no state change); an incomplete review and an approval exceeding "
    "the documented rights are each refused with no approval created",
)
def _dca_046(ctx: CheckContext) -> dict:
    """Drive the real DCA-046 source-approval store against a disposable DB, following the developer
    manual-test steps and showing actual rows (``source_term_approvals`` is a platform registry, so
    there is no tenant HTTP surface — the inspectable artifact is the approval row and the promoted
    terms status):

      1. catalogue a source (DCA-044) and record a terms VERSION (DCA-045) to review;
      2. a TENANT ADMINISTRATOR attempts the approval -> refused (not a platform operator); the
         version stays a draft and NO approval row is created;
      3. an INCOMPLETE operator review (no evidence) -> refused, nothing staged; an approval that
         tries to EXCEED the documented rights ('share', which the version denies) -> refused;
      4. a PLATFORM OPERATOR's COMPLETE review -> approved: all four essentials (evidence, reviewer,
         review date, expiry) are recorded and the version is promoted draft -> active.

    Local synthetic only (AI disabled, ephemeral DB). The source catalog entry stays a DISABLED draft
    throughout — approving the terms configuration grants no acquisition right on its own (enabling the
    source is a separate, disabled capability).
    """
    from datetime import UTC, datetime, timedelta

    from app.db.models import SourceTermsApproval, SourceTermsStatus
    from app.services.source_approval import (
        REASON_APPROVAL_EXCEEDS_RIGHTS,
        REASON_MISSING_EVIDENCE,
        REASON_NOT_PLATFORM_OPERATOR,
        ReviewerPrincipal,
        SourceApprovalAuthorizationError,
        SourceApprovalError,
        SourceApprovalReview,
        SourceApprovalStore,
    )
    from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
    from app.services.source_terms import SourceTermsInput, SourceTermsStore

    source_key = "mca_master_data"
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    operator = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
    tenant_admin = ReviewerPrincipal(subject="user-owner-A", roles=("owner", "admin"))

    def review(**overrides) -> SourceApprovalReview:
        data = {
            "reviewer_id": "op-001",
            "evidence": {"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
            "reviewed_at": now,
            "expires_at": now + timedelta(days=365),
        }
        data.update(overrides)
        return SourceApprovalReview(**data)

    out: dict = {}

    # 1) catalogue the source + record a terms version to review
    with ctx.session_factory() as session:
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=source_key,
                kind="official",
                access_method="bulk_download",
                name="MCA Company Master Data",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=source_key,
                operations={"collect": True, "store": True, "display": True, "export": True},
                field_rights={"email": {"display": True, "export": False}},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        session.commit()

    # 2) a tenant administrator cannot approve — refused, nothing changes
    with ctx.session_factory() as session:
        store = SourceApprovalStore(session)
        try:
            store.approve(source_key, 1, review(reviewer_id="user-owner-A"), tenant_admin)
            out["tenant_admin_refused"] = "UNEXPECTEDLY APPROVED"
        except SourceApprovalAuthorizationError as exc:
            out["tenant_admin_refused"] = exc.code
        session.commit()
    with ctx.session_factory() as session:
        terms = SourceTermsStore(session).get_version(source_key, 1)
        out["status_after_tenant_attempt"] = terms.status if terms else None
        out["approvals_after_tenant_attempt"] = session.query(SourceTermsApproval).count()

    # 3) incomplete review + an over-reaching approval are each refused (nothing staged)
    with ctx.session_factory() as session:
        store = SourceApprovalStore(session)
        denials: dict = {}
        try:
            store.approve(source_key, 1, review(evidence={}), operator)
            denials["no evidence"] = "UNEXPECTEDLY APPROVED"
        except SourceApprovalError as exc:
            denials["no evidence"] = exc.code
        try:
            store.approve(
                source_key, 1, review(requested_operation_rights={"share": True}), operator
            )
            denials["exceeds documented rights ('share')"] = "UNEXPECTEDLY APPROVED"
        except SourceApprovalError as exc:
            denials["exceeds documented rights ('share')"] = exc.code
        session.commit()
        out["denials"] = denials
    with ctx.session_factory() as session:
        out["approvals_after_denials"] = session.query(SourceTermsApproval).count()

    # 4) a platform operator's complete review approves and promotes the version
    with ctx.session_factory() as session:
        SourceApprovalStore(session).approve(source_key, 1, review(), operator)
        session.commit()
    with ctx.session_factory() as session:
        store = SourceApprovalStore(session)
        approval = store.approval_for(source_key, 1)
        assert approval is not None
        terms = SourceTermsStore(session).get_version(source_key, 1)
        assert terms is not None
        catalog = SourceCatalogStore(session)
        source = catalog.get(source_key)
        out["approved"] = {
            "source_key": approval.source_key,
            "terms_version": approval.terms_version,
            "reviewer_id": approval.reviewer_id,
            "reviewer_type": approval.reviewer_type,
            "evidence_reference": approval.review_evidence.get("reference"),
            "reviewed_at": approval.reviewed_at.isoformat() if approval.reviewed_at else None,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            "in_force_now": store.is_in_force(approval, now=now),
        }
        out["terms_status_after_approval"] = terms.status
        out["source_still_disabled_draft"] = source is not None and not catalog.is_usable(source)

        print("\n1) catalogued source + recorded terms version 1 (DCA-044/045).")
        print("\n2) tenant administrator attempts the approval:")
        print(
            f"   owner/admin@A -> REFUSED ({out['tenant_admin_refused']})  "
            f"terms status still '{out['status_after_tenant_attempt']}'  "
            f"approvals={out['approvals_after_tenant_attempt']}"
        )
        print("\n3) incomplete / over-reaching reviews (each refused, nothing staged):")
        for label, code in out["denials"].items():
            print(f"   {label:38} -> refused ({code})")
        print(f"   approvals after refusals: {out['approvals_after_denials']}")
        print("\n4) platform operator's COMPLETE review -> approved (actual approval row):")
        for k, v in out["approved"].items():
            print(f"     {k:20} = {v}")
        print(
            f"   endorsed terms version promoted: status now "
            f"'{out['terms_status_after_approval']}' (was 'draft')"
        )
        print(
            f"   source catalog entry still a disabled draft "
            f"(acquisition NOT enabled): {out['source_still_disabled_draft']}"
        )

    exceeds_key = "exceeds documented rights ('share')"
    no_state_change = (
        out["status_after_tenant_attempt"] == SourceTermsStatus.DRAFT.value
        and out["approvals_after_tenant_attempt"] == 0
    )
    print(
        "\nchecks: "
        f"tenant_admin_refused={out['tenant_admin_refused'] == REASON_NOT_PLATFORM_OPERATOR}  "
        f"no_state_change_on_tenant_attempt={no_state_change}  "
        f"incomplete_refused={out['denials']['no evidence'] == REASON_MISSING_EVIDENCE}  "
        f"exceeds_rights_refused={out['denials'][exceeds_key] == REASON_APPROVAL_EXCEEDS_RIGHTS}  "
        f"refusals_staged_nothing={out['approvals_after_denials'] == 0}  "
        f"operator_approved={out['approved']['reviewer_type'] == 'platform_operator'}  "
        f"version_promoted={out['terms_status_after_approval'] == SourceTermsStatus.ACTIVE.value}  "
        f"source_still_disabled={out['source_still_disabled_draft']}"
    )
    print(
        "\n(local synthetic only; no AI/paid action. Approval is a PLATFORM-operator action — a tenant "
        "administrator cannot perform it; the approval records evidence/reviewer/review-date/expiry and "
        "cannot exceed the documented rights. Promoting the terms version does NOT enable acquisition: "
        f"the source catalog entry stays a disabled draft. Table used: {SourceTermsApproval.__tablename__} "
        "— a platform control-plane registry, not tenant-scoped.)"
    )
    return out


@register(
    "DCA-047",
    "restrict source access per business: business A narrows a platform-approved source to a SUBSET "
    "of its rights (allowed), but cannot enable an unapproved source, cannot grant an operation / "
    "field right / purpose the platform denies, and an ordinary user cannot configure it; business B "
    "is untouched and each refusal stages nothing",
)
def _dca_047(ctx: CheckContext) -> dict:
    """Drive the real DCA-047 business-source-entitlement store against a disposable DB with isolated
    synthetic Businesses A/B, following the developer manual-test steps and showing actual rows:

      1. catalogue a source (DCA-044), record a terms version (DCA-045), and APPROVE it (DCA-046) so
         it is the in-force platform ceiling (collect/store/display/export source-wide — 'share'
         DENIED; email display-only; purpose 'prospecting');
      2. a business ADMINISTRATOR of A narrows the source to a SUBSET (display only) -> allowed; the
         entitlement row records exactly the narrowed rights and is audited;
      3. A attempts to BROADEN through its own configuration — enable an operation the platform denies
         ('share'), a per-field right it denies (email 'export'), a purpose it does not document
         ('resale'), and enable a source with NO platform approval — each refused, staging nothing;
      4. an ORDINARY USER of A cannot configure an entitlement at all (not an administrator);
      5. business B is never touched by A's configuration.

    Local synthetic only (AI disabled, ephemeral DB). An entitlement grants NO acquisition right on
    its own (no discovery path consults it) — it only records the narrowed rights the business opted
    into. ``business_source_entitlements`` is tenant-owned, so A's and B's rows never cross.
    """
    from app.db.models import AuditEvent, BusinessSourceEntitlement, SourceTermsApproval
    from app.identity.fake import BUSINESS_A, BUSINESS_B
    from app.services.source_approval import (
        ReviewerPrincipal,
        SourceApprovalReview,
        SourceApprovalStore,
    )
    from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
    from app.services.source_entitlement import (
        REASON_FIELD_RIGHT_DENIED_AT_PLATFORM,
        REASON_NOT_BUSINESS_ADMINISTRATOR,
        REASON_OPERATION_DENIED_AT_PLATFORM,
        REASON_PURPOSE_DENIED_AT_PLATFORM,
        REASON_SOURCE_NOT_PLATFORM_APPROVED,
        BusinessEntitler,
        BusinessSourceEntitlementAuthorizationError,
        BusinessSourceEntitlementError,
        BusinessSourceEntitlementInput,
        BusinessSourceEntitlementStore,
    )
    from app.services.source_terms import SourceTermsInput, SourceTermsStore

    from datetime import UTC, datetime, timedelta

    source_key = "mca_master_data"
    unapproved_key = "commercial_leads"
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    admin = BusinessEntitler(
        subject="00000000-0000-4000-9000-0000000a0047", roles=("owner", "admin")
    )
    ordinary = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0048", roles=("viewer",))
    operator = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))

    out: dict = {}

    # 1) catalogue + approve a source (the platform ceiling), plus an UNAPPROVED source for step 3
    with ctx.session_factory() as session:
        catalog = SourceCatalogStore(session)
        catalog.create_draft(
            SourceDraftInput(
                key=source_key,
                kind="official",
                access_method="bulk_download",
                name="MCA Company Master Data",
                india_coverage={"nationwide": True},
            )
        )
        catalog.create_draft(
            SourceDraftInput(
                key=unapproved_key,
                kind="commercial",
                access_method="api",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=source_key,
                operations={"collect": True, "store": True, "display": True, "export": True},
                field_rights={"email": {"display": True, "export": False}},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        SourceApprovalStore(session).approve(
            source_key,
            1,
            SourceApprovalReview(
                reviewer_id="op-001",
                evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
                reviewed_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=365),
            ),
            operator,
        )
        session.commit()

    # 2) business A administrator narrows to a SUBSET -> allowed
    with ctx.session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BUSINESS_A)
        entitlement = store.set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=source_key,
                enabled=True,
                operations={"display": True, "export": False},
                field_rights={"email": {"display": True}},
                purposes=["prospecting"],
            ),
            admin,
            now=now,
        )
        session.commit()
        out["A_entitlement"] = {
            "source_key": entitlement.source_key,
            "version": entitlement.version,
            "enabled": entitlement.enabled,
            "operation_rights": entitlement.operation_rights,
            "field_rights": entitlement.field_rights,
            "purposes": entitlement.purposes,
            "approved_terms_version": entitlement.approved_terms_version,
        }

    # 3) attempts to BROADEN through A's own configuration — each refused
    attempts = {
        "enable op 'share' (platform denies)": (
            BusinessSourceEntitlementInput(
                source_key=source_key, enabled=True, operations={"share": True}
            ),
            REASON_OPERATION_DENIED_AT_PLATFORM,
        ),
        "grant email 'export' (platform denies)": (
            BusinessSourceEntitlementInput(
                source_key=source_key, enabled=True, field_rights={"email": {"export": True}}
            ),
            REASON_FIELD_RIGHT_DENIED_AT_PLATFORM,
        ),
        "purpose 'resale' (not documented)": (
            BusinessSourceEntitlementInput(
                source_key=source_key, enabled=True, purposes=["resale"]
            ),
            REASON_PURPOSE_DENIED_AT_PLATFORM,
        ),
        "enable UNAPPROVED source": (
            BusinessSourceEntitlementInput(source_key=unapproved_key, enabled=True),
            REASON_SOURCE_NOT_PLATFORM_APPROVED,
        ),
    }
    denials: dict = {}
    with ctx.session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BUSINESS_A)
        for label, (data, expected) in attempts.items():
            try:
                store.set_entitlement(data, admin, now=now)
                denials[label] = "UNEXPECTEDLY ALLOWED"
            except BusinessSourceEntitlementError as exc:
                denials[label] = exc.code
        session.commit()
    out["broaden_denials"] = denials

    # 4) an ordinary (non-admin) user cannot configure an entitlement
    with ctx.session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BUSINESS_A)
        try:
            store.set_entitlement(
                BusinessSourceEntitlementInput(source_key=source_key, enabled=True),
                ordinary,
                now=now,
            )
            out["ordinary_user"] = "UNEXPECTEDLY ALLOWED"
        except BusinessSourceEntitlementAuthorizationError as exc:
            out["ordinary_user"] = exc.code
        session.commit()

    # 5) business B is untouched; only A's single narrowed version exists; each refusal staged nothing
    with ctx.session_factory() as session:
        store_a = BusinessSourceEntitlementStore(session, BUSINESS_A)
        store_b = BusinessSourceEntitlementStore(session, BUSINESS_B)
        out["A_versions"] = [e.version for e in store_a.versions(source_key)]
        out["B_has_entitlement"] = store_b.latest(source_key) is not None
        out["entitlement_rows_total"] = session.query(BusinessSourceEntitlement).count()
        out["approval_rows_total"] = session.query(SourceTermsApproval).count()
        # A's narrowed read is the intersection: display allowed, export (approved but not entitled) denied.
        out["A_permits_email_display"] = store_a.permits(source_key, "email", "display", now=now)
        out["A_permits_name_export"] = store_a.permits(source_key, "name", "export", now=now)
        out["B_permits_email_display"] = store_b.permits(source_key, "email", "display", now=now)
        audits = session.query(AuditEvent).filter(AuditEvent.business_id == BUSINESS_A).all()
        out["A_audit_actions"] = [a.action for a in audits]

    print(
        "\n1) catalogued + APPROVED source (platform ceiling): "
        "collect/store/display/export, 'share' DENIED, email display-only, purpose 'prospecting'."
    )
    print("\n2) business A administrator narrows to a SUBSET -> ALLOWED:")
    for k, v in out["A_entitlement"].items():
        print(f"     {k:22} = {v}")
    print(
        "\n3) A attempts to BROADEN through its own configuration (each refused, nothing staged):"
    )
    for label, code in out["broaden_denials"].items():
        print(f"   {label:40} -> refused ({code})")
    print(
        f"\n4) ordinary (non-admin) user configures entitlement -> refused ({out['ordinary_user']})"
    )
    print("\n5) isolation + no stray state:")
    print(
        f"   A entitlement versions           : {out['A_versions']} (only the one narrowed version)"
    )
    print(
        f"   B has an entitlement             : {out['B_has_entitlement']} (A's config never touched B)"
    )
    print(f"   total entitlement rows           : {out['entitlement_rows_total']}")
    print(f"   A permits (email, display)       : {out['A_permits_email_display']}")
    print(
        f"   A permits (name, export)         : {out['A_permits_name_export']} "
        "(approved at platform but NOT entitled by A -> narrowed out)"
    )
    print(
        f"   B permits (email, display)       : {out['B_permits_email_display']} (B not entitled)"
    )
    print(f"   A audit actions                  : {out['A_audit_actions']}")

    all_denied = (
        out["broaden_denials"]["enable op 'share' (platform denies)"]
        == REASON_OPERATION_DENIED_AT_PLATFORM
        and out["broaden_denials"]["grant email 'export' (platform denies)"]
        == REASON_FIELD_RIGHT_DENIED_AT_PLATFORM
        and out["broaden_denials"]["purpose 'resale' (not documented)"]
        == REASON_PURPOSE_DENIED_AT_PLATFORM
        and out["broaden_denials"]["enable UNAPPROVED source"]
        == REASON_SOURCE_NOT_PLATFORM_APPROVED
    )
    print(
        "\nchecks: "
        f"narrowing_allowed={out['A_entitlement']['version'] == 1}  "
        f"all_broadening_refused={all_denied}  "
        f"ordinary_user_refused={out['ordinary_user'] == REASON_NOT_BUSINESS_ADMINISTRATOR}  "
        f"only_one_row_staged={out['entitlement_rows_total'] == 1}  "
        f"B_untouched={out['B_has_entitlement'] is False}"
    )
    print(
        "\n(local synthetic only; no AI/paid action. A business can only NARROW the platform-approved "
        "rights, never widen them: it cannot enable an unapproved source or grant an operation/field/"
        "purpose the platform denied. Entitling grants no acquisition right on its own — no discovery "
        f"path consults {BusinessSourceEntitlement.__tablename__}. The table is tenant-owned, so A and "
        "B never cross.)"
    )
    return out


@register(
    "DCA-048",
    "authorize a source operation before fetching: a fully-authorized op ALLOWS and reaches a counted "
    "fake adapter, while every denial (unknown/draft source, no platform approval, operation/purpose "
    "denied or not entitled) makes ZERO adapter calls — and an official-origin + robots-allowed source "
    "with no rights is still denied (origin/robots cannot bypass rights)",
)
def _dca_048(ctx: CheckContext) -> dict:
    """Drive the real DCA-048 source preflight against a disposable DB with isolated synthetic
    Businesses A/B, following the developer manual-test steps and showing actual decisions + the
    fake adapter's call count:

      1. catalogue a source (DCA-044), record terms (DCA-045), APPROVE it (DCA-046) and — for the
         ALLOW path only — synthetically mark the catalog entry ENABLED (nothing in the service does
         this; it is a test-only enable so the allow branch is reachable). Business A entitles a
         SUBSET (DCA-047): collect/display, purpose 'prospecting';
      2. request ALLOWED fields/operations through a COUNTED fake adapter and show it IS reached;
      3. request PROHIBITED operations/purposes and an UNAPPROVED / DRAFT source, each with an
         'official origin' + 'robots.txt allows' hint — every one is DENIED with a stable reason and
         the adapter is NEVER called (official origin / robots cannot bypass the rights chain);
      4. business B (same platform ceiling, no entitlement) is denied; re-running changes nothing.

    Local synthetic only (AI disabled, ephemeral DB). The preflight is a pure read: it stages
    nothing and authorizes no acquisition — in the running service every fetch is denied at the
    source-status gate because no source is ever ENABLED.
    """
    from datetime import UTC, datetime, timedelta

    from app.db.models import AuditEvent, BusinessSourceEntitlement, Source, SourceStatus
    from app.identity.fake import BUSINESS_A, BUSINESS_B
    from app.services.source_approval import (
        ReviewerPrincipal,
        SourceApprovalReview,
        SourceApprovalStore,
    )
    from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
    from app.services.source_entitlement import (
        BusinessEntitler,
        BusinessSourceEntitlementInput,
        BusinessSourceEntitlementStore,
    )
    from app.services.source_preflight import PreflightRequest, SourcePreflight
    from app.services.source_terms import SourceTermsInput, SourceTermsStore

    source_key = "mca_master_data"
    unapproved_key = "commercial_leads"
    draft_key = "mca_draft_only"
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    admin = BusinessEntitler(
        subject="00000000-0000-4000-9000-0000000a0048", roles=("owner", "admin")
    )
    operator = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
    # An "official origin" source whose robots.txt permits crawling — advisory hints the gate IGNORES.
    official_robots = {"official_origin": True, "robots_allowed": True, "trust_tier": "official"}

    class CountingAdapter:
        key = source_key

        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, icp: dict) -> list:
            self.calls += 1
            return [{"org_name": "Synthetic Textiles Pvt Ltd", "source_key": self.key}]

    out: dict = {}

    # 1) platform ceiling: an APPROVED + (test-only) ENABLED source, plus an UNAPPROVED and a DRAFT one
    with ctx.session_factory() as session:
        catalog = SourceCatalogStore(session)
        catalog.create_draft(
            SourceDraftInput(
                key=source_key,
                kind="official",
                access_method="bulk_download",
                name="MCA Company Master Data",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        catalog.create_draft(
            SourceDraftInput(
                key=unapproved_key,
                # An OFFICIAL-origin registry with a permissive robots.txt — but NO platform approval.
                kind="official",
                access_method="bulk_download",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        catalog.create_draft(
            SourceDraftInput(
                key=draft_key,
                kind="official",
                access_method="bulk_download",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=source_key,
                operations={"collect": True, "store": True, "display": True, "export": True},
                field_rights={"email": {"display": True, "export": False}},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        SourceApprovalStore(session).approve(
            source_key,
            1,
            SourceApprovalReview(
                reviewer_id="op-001",
                evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
                reviewed_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=365),
            ),
            operator,
        )
        # Test-only synthetic enable so the ALLOW branch is reachable (the service never does this).
        enabled = session.query(Source).filter(Source.key == source_key).one()
        enabled.status = SourceStatus.ENABLED.value
        enabled.enabled = True
        # The official-origin UNAPPROVED source is ALSO (test-only) enabled, so the ONLY thing it
        # lacks is a platform approval — proving an official origin + permissive robots.txt cannot,
        # on their own, bypass the rights chain (the preflight denies 'source_not_platform_approved').
        unapproved = session.query(Source).filter(Source.key == unapproved_key).one()
        unapproved.status = SourceStatus.ENABLED.value
        unapproved.enabled = True
        # Business A entitles a SUBSET: collect + display, purpose 'prospecting' ('export'/'share' not).
        BusinessSourceEntitlementStore(session, BUSINESS_A).set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=source_key,
                enabled=True,
                operations={"collect": True, "display": True},
                field_rights={"email": {"display": True}},
                purposes=["prospecting"],
            ),
            admin,
            now=now,
        )
        session.commit()

    # 2) ALLOWED request reaches the counted adapter
    with ctx.session_factory() as session:
        gate = SourcePreflight(session, BUSINESS_A)
        adapter = CountingAdapter()
        allowed = gate.fetch(
            adapter,
            PreflightRequest(
                source_key=source_key,
                operation="collect",
                purpose="prospecting",
                origin_signals=official_robots,
            ),
            icp={"industries": ["textiles"]},
            now=now,
        )
        out["allowed"] = {
            "decision": allowed.decision.allowed,
            "reason": allowed.decision.reason,
            "adapter_called": allowed.adapter_called,
            "adapter_calls": adapter.calls,
            "candidates": allowed.candidates,
        }

    # 3) PROHIBITED requests — each DENIED, each making ZERO adapter calls
    prohibited = {
        "op 'export' (approved but not entitled by A)": PreflightRequest(
            source_key=source_key,
            operation="export",
            purpose="prospecting",
            origin_signals=official_robots,
        ),
        "op 'share' (denied at platform)": PreflightRequest(
            source_key=source_key,
            operation="share",
            purpose="prospecting",
            origin_signals=official_robots,
        ),
        "purpose 'resale' (not documented)": PreflightRequest(
            source_key=source_key,
            operation="collect",
            purpose="resale",
            origin_signals=official_robots,
        ),
        "UNAPPROVED source (official origin + robots ok)": PreflightRequest(
            source_key=unapproved_key,
            operation="collect",
            purpose="prospecting",
            origin_signals=official_robots,
        ),
        "DRAFT source (not enabled)": PreflightRequest(
            source_key=draft_key,
            operation="collect",
            purpose="prospecting",
            origin_signals=official_robots,
        ),
        "unknown source": PreflightRequest(
            source_key="no_such_source",
            operation="collect",
            purpose="prospecting",
            origin_signals=official_robots,
        ),
    }
    denials: dict = {}
    total_calls = 0
    with ctx.session_factory() as session:
        gate = SourcePreflight(session, BUSINESS_A)
        for label, req in prohibited.items():
            adapter = CountingAdapter()
            result = gate.fetch(adapter, req, icp={}, now=now)
            total_calls += adapter.calls
            denials[label] = {
                "reason": result.decision.reason,
                "adapter_called": result.adapter_called,
                "adapter_calls": adapter.calls,
            }
    out["denials"] = denials
    out["denied_adapter_calls_total"] = total_calls

    # 4) tenant isolation: B shares the ceiling but has no entitlement -> denied; A re-run is stable
    with ctx.session_factory() as session:
        req = PreflightRequest(source_key=source_key, operation="collect", purpose="prospecting")
        a_first = SourcePreflight(session, BUSINESS_A).authorize(req, now=now)
        a_second = SourcePreflight(session, BUSINESS_A).authorize(req, now=now)
        b_decision = SourcePreflight(session, BUSINESS_B).authorize(req, now=now)
        out["A_allowed_repeatable"] = (a_first.allowed, a_second.allowed)
        out["B_decision"] = (b_decision.allowed, b_decision.reason)
        out["entitlement_rows_total"] = session.query(BusinessSourceEntitlement).count()
        out["audit_actions"] = [a.action for a in session.query(AuditEvent).all()]

    print("\n1) platform ceiling: APPROVED + (test-only) ENABLED 'mca_master_data'")
    print("   A entitles a SUBSET: collect/display, purpose 'prospecting' (export/share NOT).")
    print("\n2) ALLOWED request (collect / prospecting, official-origin + robots hint):")
    a = out["allowed"]
    print(
        f"   decision={'ALLOW' if a['decision'] else 'DENY'}  adapter_called={a['adapter_called']}  "
        f"adapter_calls={a['adapter_calls']}  candidates={a['candidates']}"
    )
    print("\n3) PROHIBITED requests (each DENIED, adapter NEVER called):")
    for label, info in denials.items():
        print(
            f"   {label:48} -> {info['reason']:30} "
            f"adapter_called={info['adapter_called']} calls={info['adapter_calls']}"
        )
    print(f"\n   total adapter calls across ALL denied requests: {total_calls} (must be 0)")
    print("\n4) tenant isolation + reproducibility:")
    print(f"   A allowed on re-run       : {out['A_allowed_repeatable']} (pure read, stable)")
    print(f"   B (no entitlement)        : {out['B_decision']}")
    print(
        f"   entitlement rows total    : {out['entitlement_rows_total']} (preflight staged nothing)"
    )
    print(
        f"   audit actions             : {out['audit_actions']} (only the DCA-047 entitlement set)"
    )
    print(
        "\nchecks: "
        f"allowed_reached_adapter={a['decision'] and a['adapter_called'] and a['adapter_calls'] == 1}  "
        f"all_denials_zero_calls={total_calls == 0}  "
        f"official_robots_did_not_bypass="
        f"{denials['UNAPPROVED source (official origin + robots ok)']['reason'] == 'source_not_platform_approved'}  "
        f"B_denied={out['B_decision'][0] is False}"
    )
    print(
        "\n(local synthetic only; no AI/paid action. A denied preflight makes ZERO adapter calls and "
        "an official origin or permissive robots.txt cannot bypass the rights chain. The preflight is "
        "a pure read — it authorizes no acquisition and, in the running service, denies every fetch at "
        "the source-status gate because no source is ever ENABLED.)"
    )
    return out


@register(
    "DCA-049",
    "expire source access on time: a controllable clock crosses the approval's review deadline — the "
    "preflight ALLOWS before expiry and DENIES the instant after (while the row is still stored "
    "'active', i.e. BEFORE any sweep), then the scheduled expiry sweep durably stamps approval+terms "
    "EXPIRED; a not-yet-effective approval is denied but never swept, and an in-force one is untouched",
)
def _dca_049(ctx: CheckContext) -> dict:
    """Drive the real DCA-049 effective-date check + scheduled expiry sweep against a disposable DB
    with isolated synthetic Businesses A/B, following the developer manual-test steps:

      1. catalogue a source (DCA-044), record terms (DCA-045), APPROVE it with an EXPLICIT review
         deadline (DCA-046), (test-only) ENABLE the catalog entry and entitle A (DCA-047);
      2. with a CONTROLLABLE clock, authorize the same preflight BEFORE expiry (ALLOW) and the instant
         AFTER expiry (DENY) — and show the approval row is STILL stored 'active', proving the source
         became unusable by the effective-date check alone, BEFORE any sweep ran;
      3. run the scheduled expiry sweep at the post-expiry clock and show it durably stamped the
         approval AND the terms it promoted EXPIRED (idempotent on a second run);
      4. a not-yet-effective approval (effective_from in the future) is DENIED by the effective-date
         check yet is NOT expired by the sweep; an in-force approval is left ACTIVE and usable.

    Local synthetic only (AI disabled, ephemeral DB). The preflight is a pure read and the sweep only
    moves a lapsed right MORE restrictive — nothing here enables acquisition (every production fetch
    still denies at the source-status gate because no source is ever ENABLED).
    """
    from datetime import UTC, datetime, timedelta

    from app.db.models import (
        Source,
        SourceApprovalStatus,
        SourceStatus,
        SourceTerms,
        SourceTermsApproval,
        SourceTermsStatus,
    )
    from app.identity.fake import BUSINESS_A, BUSINESS_B
    from app.services.source_approval import (
        ReviewerPrincipal,
        SourceApprovalReview,
        SourceApprovalStore,
    )
    from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
    from app.services.source_entitlement import (
        BusinessEntitler,
        BusinessSourceEntitlementInput,
        BusinessSourceEntitlementStore,
    )
    from app.services.source_expiry import SourceExpirySweep
    from app.services.source_preflight import PreflightRequest, SourcePreflight
    from app.services.source_terms import SourceTermsInput, SourceTermsStore

    lapsing_key = "mca_master_data"  # approval expires mid-test (the review deadline)
    pending_key = "future_registry"  # approval not yet effective
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    expires_at = now + timedelta(days=30)  # the review deadline the clock crosses
    after_expiry = expires_at + timedelta(seconds=1)  # controllable clock, just past the deadline
    admin = BusinessEntitler(
        subject="00000000-0000-4000-9000-0000000a0049", roles=("owner", "admin")
    )
    operator = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
    req_lapsing = PreflightRequest(
        source_key=lapsing_key, operation="collect", purpose="prospecting"
    )
    req_pending = PreflightRequest(
        source_key=pending_key, operation="collect", purpose="prospecting"
    )

    def _catalogue_approve_enable(session, key, *, effective_from, expires):
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=key,
                kind="official",
                access_method="bulk_download",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=key,
                operations={"collect": True, "display": True},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        SourceApprovalStore(session).approve(
            key,
            1,
            SourceApprovalReview(
                reviewer_id="op-001",
                evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
                reviewed_at=now - timedelta(days=1),
                effective_from=effective_from,
                expires_at=expires,
            ),
            operator,
        )
        source = session.query(Source).filter(Source.key == key).one()
        source.status = SourceStatus.ENABLED.value
        source.enabled = True

    out: dict = {}

    # 1) seed: a source approved with a 30-day review deadline, plus a not-yet-effective source.
    with ctx.session_factory() as session:
        _catalogue_approve_enable(session, lapsing_key, effective_from=None, expires=expires_at)
        _catalogue_approve_enable(
            session,
            pending_key,
            effective_from=now + timedelta(days=5),  # not yet effective at `now`
            expires=now + timedelta(days=90),
        )
        # Entitle A on both (the pending one is entitled at a moment it IS in force).
        BusinessSourceEntitlementStore(session, BUSINESS_A).set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=lapsing_key,
                enabled=True,
                operations={"collect": True, "display": True},
                purposes=["prospecting"],
            ),
            admin,
            now=now,
        )
        BusinessSourceEntitlementStore(session, BUSINESS_A).set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=pending_key,
                enabled=True,
                operations={"collect": True},
                purposes=["prospecting"],
            ),
            admin,
            now=now + timedelta(days=6),  # after the pending approval's effective_from
        )
        session.commit()

    # 2) controllable clock crosses the deadline — DENY before any sweep, row still 'active'.
    with ctx.session_factory() as session:
        gate = SourcePreflight(session, BUSINESS_A)
        before = gate.authorize(req_lapsing, now=now)
        after = gate.authorize(req_lapsing, now=after_expiry)
        status_before_sweep = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == lapsing_key)
            .one()
            .status
        )
    out["before_expiry"] = {"allowed": before.allowed, "reason": before.reason}
    out["after_expiry_before_sweep"] = {"allowed": after.allowed, "reason": after.reason}
    out["approval_status_before_sweep"] = status_before_sweep

    # 3) scheduled expiry sweep at the post-expiry clock — durable EXPIRED, idempotent.
    with ctx.session_factory() as session:
        first = SourceExpirySweep(session).sweep(now=after_expiry)
        session.commit()
    with ctx.session_factory() as session:
        second = SourceExpirySweep(session).sweep(now=after_expiry)
        session.commit()
    with ctx.session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == lapsing_key)
            .one()
        )
        terms = session.query(SourceTerms).filter(SourceTerms.source_key == lapsing_key).one()
        post = SourcePreflight(session, BUSINESS_A).authorize(req_lapsing, now=after_expiry)
    out["sweep_first"] = {
        "approvals": first.expired_approvals,
        "terms": first.expired_terms,
    }
    out["sweep_second_transitioned"] = second.transitioned
    out["approval_status_after_sweep"] = approval.status
    out["terms_status_after_sweep"] = terms.status
    out["after_sweep_preflight"] = {"allowed": post.allowed, "reason": post.reason}

    # 4) not-yet-effective approval: DENIED by the effective-date check, NOT expired by the sweep.
    with ctx.session_factory() as session:
        pending_decision = SourcePreflight(session, BUSINESS_A).authorize(req_pending, now=now)
        swept = SourceExpirySweep(session).sweep(now=now)
        session.commit()
    with ctx.session_factory() as session:
        pending_status = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == pending_key)
            .one()
            .status
        )
    out["pending_decision"] = {
        "allowed": pending_decision.allowed,
        "reason": pending_decision.reason,
    }
    out["pending_swept_transitioned"] = swept.transitioned
    out["pending_status"] = pending_status
    # Tenant isolation: B never entitled either source.
    with ctx.session_factory() as session:
        b_decision = SourcePreflight(session, BUSINESS_B).authorize(req_lapsing, now=now)
    out["B_decision"] = {"allowed": b_decision.allowed, "reason": b_decision.reason}

    print("\n1) seeded 'mca_master_data' approved with a 30-day review deadline; A entitled.")
    print("\n2) controllable clock crosses the review deadline (BEFORE any sweep):")
    print(
        f"   before expiry (now)        -> {'ALLOW' if before.allowed else 'DENY'}  "
        f"reason={before.reason}"
    )
    print(
        f"   after expiry  (+30d +1s)   -> {'ALLOW' if after.allowed else 'DENY'}  "
        f"reason={after.reason}"
    )
    print(
        f"   approval row status now    -> {status_before_sweep!r}  "
        "(STILL 'active' — denial enforced by the effective-date check, not the sweep)"
    )
    print("\n3) scheduled expiry sweep at the post-expiry clock:")
    print(f"   sweep #1 expired approvals={first.expired_approvals} terms={first.expired_terms}")
    print(f"   sweep #2 transitioned      -> {second.transitioned} (idempotent — nothing left)")
    print(f"   durable status             -> approval={approval.status!r} terms={terms.status!r}")
    print(
        f"   preflight after sweep      -> {'ALLOW' if post.allowed else 'DENY'}  "
        f"reason={post.reason}"
    )
    print("\n4) not-yet-effective approval ('future_registry'):")
    print(
        f"   preflight at now           -> {'ALLOW' if pending_decision.allowed else 'DENY'}  "
        f"reason={pending_decision.reason}  (not yet effective)"
    )
    print(
        f"   sweep transitioned         -> {swept.transitioned} "
        f"(NOT lapsed); status stays {pending_status!r}"
    )
    print(
        f"   B (no entitlement)         -> {'ALLOW' if b_decision.allowed else 'DENY'}  "
        f"reason={b_decision.reason}"
    )
    checks = {
        "allowed_before_expiry": before.allowed is True,
        "denied_after_expiry": after.allowed is False,
        "unusable_before_sweep": status_before_sweep == SourceApprovalStatus.ACTIVE.value,
        "sweep_stamped_expired": approval.status == SourceApprovalStatus.EXPIRED.value
        and terms.status == SourceTermsStatus.EXPIRED.value,
        "sweep_idempotent": second.transitioned == 0,
        "pending_denied_not_swept": pending_decision.allowed is False
        and swept.transitioned == 0
        and pending_status == SourceApprovalStatus.ACTIVE.value,
    }
    print(f"\nchecks: {checks}")
    out["checks"] = checks
    print(
        "\n(local synthetic only; no AI/paid action. An expired source is unusable the moment its "
        "window closes — the effective-date check denies BEFORE the scheduled sweep stamps the "
        "durable EXPIRED state. The sweep only records the lapse; it never enables acquisition.)"
    )
    return out


@register(
    "DCA-050",
    "revoke a source version: a running task authorizes against the old version (ALLOW) and is paused "
    "mid-fetch; an operator revokes that version; the task resumes and its re-check before commit/use "
    "is DENIED (same clock, no sweep) while a tenant admin is refused the revoke; the scheduled "
    "invalidation sweep then flips the observations acquired from the revoked source stale (idempotent)",
)
def _dca_050(ctx: CheckContext) -> dict:
    """Drive the real DCA-050 operator revocation + queued invalidation against a disposable DB with
    isolated synthetic Businesses A/B, following the developer manual-test steps:

      1. catalogue a source (DCA-044), record terms (DCA-045), APPROVE it (DCA-046), (test-only) ENABLE
         the catalog entry and entitle A (DCA-047); seed one observation A acquired from that source;
      2. a running task authorizes against the in-force source version (ALLOW) and captures it, then is
         PAUSED mid-fetch (it holds candidates it has not yet committed);
      3. a platform operator REVOKES that version (a tenant admin is refused first — Admin role alone is
         insufficient); the task RESUMES and re-checks before committing — at the SAME clock — and is
         DENIED (the preflight and the version-specific in-force check both refuse), so it commits
         nothing: revoked rights cannot be used by an already-running task with an old source version;
      4. the scheduled invalidation sweep flips A's observation from the revoked source current -> stale
         (queued for a future, disabled re-review), idempotent on a second run; B (never entitled) is
         unaffected.

    Local synthetic only (AI disabled, ephemeral DB). Nothing here enables acquisition — revocation only
    ever makes a right MORE restrictive (every production fetch still denies at the source-status gate).
    """
    from datetime import UTC, datetime, timedelta

    from app.db.models import (
        DiscoveryRun,
        ProspectCandidate,
        ScoreStatus,
        Source,
        SourceApprovalStatus,
        SourceStatus,
        SourceTerms,
        SourceTermsStatus,
    )
    from app.identity.fake import BUSINESS_A, BUSINESS_B
    from app.services.source_approval import (
        ReviewerPrincipal,
        SourceApprovalReview,
        SourceApprovalStore,
    )
    from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
    from app.services.source_entitlement import (
        BusinessEntitler,
        BusinessSourceEntitlementInput,
        BusinessSourceEntitlementStore,
    )
    from app.services.source_preflight import PreflightRequest, SourcePreflight
    from app.services.source_revocation import (
        RevocationInvalidationSweep,
        RevokerPrincipal,
        SourceRevocationAuthorizationError,
        SourceRevocationStore,
    )
    from app.services.source_terms import SourceTermsInput, SourceTermsStore

    source_key = "mca_master_data"
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    admin = BusinessEntitler(
        subject="00000000-0000-4000-9000-0000000a0050", roles=("owner", "admin")
    )
    operator = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
    revoker = RevokerPrincipal(subject="op-001", roles=("platform_operator",))
    tenant_admin = RevokerPrincipal(subject="tenant-admin-1", roles=("owner", "admin"))
    req = PreflightRequest(source_key=source_key, operation="collect", purpose="prospecting")

    class PausableAdapter:
        """A fake source adapter whose fetch is 'paused' (returns candidates not yet committed)."""

        key = source_key

        def fetch(self, icp: dict) -> list:
            return [{"org_name": "Synthetic Textiles Pvt Ltd", "source_key": self.key}]

    out: dict = {}

    # 1) seed: approved + (test-only) enabled source, A entitled, one observation A acquired from it.
    with ctx.session_factory() as session:
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=source_key,
                kind="official",
                access_method="bulk_download",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=source_key,
                operations={"collect": True, "display": True},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        SourceApprovalStore(session).approve(
            source_key,
            1,
            SourceApprovalReview(
                reviewer_id="op-001",
                evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
                reviewed_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=365),
            ),
            operator,
        )
        source = session.query(Source).filter(Source.key == source_key).one()
        source.status = SourceStatus.ENABLED.value
        source.enabled = True
        BusinessSourceEntitlementStore(session, BUSINESS_A).set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=source_key,
                enabled=True,
                operations={"collect": True, "display": True},
                purposes=["prospecting"],
            ),
            admin,
            now=now,
        )
        run = DiscoveryRun(business_id=BUSINESS_A, source_keys=[source_key], status="completed")
        session.add(run)
        session.flush()
        candidate = ProspectCandidate(
            business_id=BUSINESS_A,
            run_id=run.id,
            org_name="Synthetic Textiles Pvt Ltd",
            org_identifier="CIN-SYNTH-1",
            contact={},
            source_key=source_key,
            source_ref="synthetic://record/1",
            trust_tier="official_registry",
            match_score=1.0,
            rationale="matches ICP",
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
        session.commit()

    # 2) the running task authorizes against the in-force version (ALLOW) and is PAUSED mid-fetch.
    with ctx.session_factory() as session:
        gate = SourcePreflight(session, BUSINESS_A)
        start = gate.authorize(req, now=now)
        captured_version = SourceApprovalStore(session).approvals(source_key)[0].terms_version
        paused_candidates = PausableAdapter().fetch({"industries": ["textiles"]})
    out["task_start"] = {"allowed": start.allowed, "captured_version": captured_version}

    # 3a) a tenant admin is refused the revoke (Admin role alone is insufficient — platform scope).
    tenant_refused = False
    with ctx.session_factory() as session:
        try:
            SourceRevocationStore(session).revoke(
                source_key, captured_version, tenant_admin, reason="tenant_tried", now=now
            )
        except SourceRevocationAuthorizationError as exc:
            tenant_refused = True
            out["tenant_admin_revoke"] = {"refused": True, "code": exc.code}
        session.rollback()

    # 3b) the platform operator revokes the version the task is holding.
    with ctx.session_factory() as session:
        result = SourceRevocationStore(session).revoke(
            source_key, captured_version, revoker, reason="rights_withdrawn_by_licensor", now=now
        )
        session.commit()
    out["revocation"] = {
        "terms_version": result.terms_version,
        "revoked_by": result.revoked_by,
        "demoted_terms": result.demoted_terms,
    }

    # 3c) the task RESUMES and re-checks before commit/use — SAME clock, no sweep — and is DENIED.
    with ctx.session_factory() as session:
        gate = SourcePreflight(session, BUSINESS_A)
        resume = gate.authorize(req, now=now)
        approval = SourceApprovalStore(session).approval_for(source_key, captured_version)
        version_in_force = SourceApprovalStore.is_in_force(approval, now=now)
        stored_status = approval.status
        terms_status = (
            session.query(SourceTerms).filter(SourceTerms.source_key == source_key).one().status
        )
    out["task_resume"] = {
        "allowed": resume.allowed,
        "reason": resume.reason,
        "version_in_force": version_in_force,
        "approval_status": stored_status,
        "terms_status": terms_status,
        "paused_candidates_discarded": len(paused_candidates),  # fetched, never committed
    }

    # 4) the scheduled invalidation sweep flips A's observation from the revoked source stale; B's data
    #    (never entitled / none) is unaffected. Idempotent on a second run.
    with ctx.session_factory() as session:
        first = RevocationInvalidationSweep(session).sweep(now=now)
        session.commit()
    with ctx.session_factory() as session:
        second = RevocationInvalidationSweep(session).sweep(now=now)
        session.commit()
    with ctx.session_factory() as session:
        candidate = session.get(ProspectCandidate, candidate_id)
        out["invalidation"] = {
            "revoked_source_keys": first.revoked_source_keys,
            "invalidated_candidate_ids": first.invalidated_candidate_ids,
            "second_run_invalidated": second.invalidated,
            "candidate_score_status": candidate.score_status,
            "candidate_status": candidate.status,  # approval/acceptance untouched (score-only)
        }
        # Tenant isolation: B was never entitled this source and holds no observations from it.
        b_rows = (
            session.query(ProspectCandidate)
            .filter(ProspectCandidate.business_id == BUSINESS_B)
            .count()
        )
        out["B_candidate_rows"] = b_rows

    print("\n1) seeded 'mca_master_data' approved+ (test-only) ENABLED; A entitled; 1 observation.")
    print("\n2) running task authorizes against the in-force version (paused mid-fetch):")
    print(
        f"   authorize -> {'ALLOW' if start.allowed else 'DENY'}  "
        f"captured source version = {captured_version}  "
        f"(fetched {len(paused_candidates)} candidate(s), NOT yet committed)"
    )
    print("\n3) revoke the version while the task is paused:")
    print(
        f"   tenant admin revoke       -> {'REFUSED' if tenant_refused else 'ALLOWED?!'} "
        f"({out.get('tenant_admin_revoke', {}).get('code')})  (Admin role alone is insufficient)"
    )
    print(
        f"   operator revoke v{captured_version}         -> demoted_terms={result.demoted_terms} "
        f"revoked_by={result.revoked_by!r}"
    )
    print("   task RESUMES and re-checks before commit/use (SAME clock, no sweep):")
    print(
        f"     preflight re-check      -> {'ALLOW' if resume.allowed else 'DENY'}  "
        f"reason={resume.reason}"
    )
    print(
        f"     old version in force?   -> {version_in_force}  "
        f"(approval={stored_status!r}, terms={terms_status!r})"
    )
    print("\n4) scheduled invalidation sweep (queued invalidation of affected observations):")
    print(
        f"   revoked source keys        -> {first.revoked_source_keys}  "
        f"invalidated candidates -> {first.invalidated_candidate_ids}"
    )
    print(
        f"   sweep #2 invalidated       -> {second.invalidated} (idempotent)  "
        f"candidate score_status={out['invalidation']['candidate_score_status']!r} "
        f"status={out['invalidation']['candidate_status']!r} (approval untouched)"
    )
    print(f"   B observations from source -> {out['B_candidate_rows']} (tenant isolation)")
    checks = {
        "task_started_allowed": start.allowed is True,
        "tenant_admin_refused": tenant_refused is True,
        "running_task_denied_after_revoke": resume.allowed is False and version_in_force is False,
        "approval_and_terms_revoked": stored_status == SourceApprovalStatus.REVOKED.value
        and terms_status == SourceTermsStatus.REVOKED.value,
        "observation_invalidated": out["invalidation"]["candidate_score_status"]
        == ScoreStatus.STALE.value,
        "invalidation_idempotent": second.invalidated == 0,
    }
    print(f"\nchecks: {checks}")
    out["checks"] = checks
    print(
        "\n(local synthetic only; no AI/paid action. Revoked rights cannot be used by an already-"
        "running task holding the old source version — the re-check denies the instant the approval is "
        "revoked, with no sweep required. The invalidation sweep only marks affected observations stale; "
        "it enables nothing.)"
    )
    return out


@register(
    "DCA-052",
    "versioned India rulepack schemas: a complete fixture (and one with no effective dates) validates "
    "to a DRAFT row; a contradictory fixture and a malformed (non-India / no evidence) fixture are "
    "refused with a stable code and stage NO row; nothing is ever live",
)
def _dca_052(ctx: CheckContext) -> dict:
    """Drive the real DCA-052 policy-rulepack validation + store against a disposable DB, following the
    developer manual-test steps:

      1. VALIDATE a complete India fixture (actions, channels, conditions, evidence, effective dates,
         law/provider/product authority) — it stages exactly ONE row, ``status='draft'`` (never live);
      2. VALIDATE a fixture whose rule has NO effective dates — a valid, unbounded rule (version 2);
      3. a CONTRADICTORY fixture (same action/channel/authority/conditions, allow AND deny, overlapping
         window) is REFUSED with ``contradictory_rules`` and stages NO row;
      4. a MALFORMED fixture (non-India jurisdiction; and separately, a rule missing evidence) is
         REFUSED with a stable code and stages NO row.

    Control-plane data (no tenant scope / no RLS). Local synthetic only; AI disabled; nothing here
    activates a policy — every stored pack is a reviewable draft and enforces nothing.
    """
    from datetime import UTC, datetime, timedelta

    from app.compliance.policy_rulepacks import (
        REASON_CONTRADICTORY_RULES,
        REASON_INVALID_JURISDICTION,
        REASON_MISSING_RULE_EVIDENCE,
        CompliancePolicyRulepackStore,
        PolicyRuleInput,
        PolicyRulepackValidationError,
        RulepackInput,
    )
    from app.db.models import CompliancePolicyRulepack, PolicyRulepackStatus

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    evidence = {"reference": "DPDP-2023-s7", "kind": "statute", "uri": "https://example.test/law"}

    def store_pack(pack: RulepackInput):
        with ctx.session_factory() as session:
            result = CompliancePolicyRulepackStore(session).add_version(pack)
            session.commit()
            return result.id, result.version

    def refuse(pack: RulepackInput) -> str | None:
        with ctx.session_factory() as session:
            try:
                CompliancePolicyRulepackStore(session).add_version(pack)
                session.commit()
                return None  # unexpectedly accepted
            except PolicyRulepackValidationError as exc:
                session.rollback()
                return exc.code

    def count() -> int:
        with ctx.session_factory() as session:
            return len(CompliancePolicyRulepackStore(session).versions("IN"))

    out: dict = {}

    # 1) a complete, valid India fixture — one allow (law) and one deny (product) rule.
    complete = RulepackInput(
        jurisdiction="IN",
        title="India baseline policy",
        evidence={"reference": "POLICY-REVIEW-2026-11", "kind": "legal_review"},
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required", "trust_tier": ["official_registry"]},
                evidence=evidence,
                effective_from=now,
                expires_at=now + timedelta(days=365),
            ),
            PolicyRuleInput(
                effect="deny",
                action="contact",
                channel="sms",
                authority="product",
                conditions={"registration_required": True},
                evidence={"reference": "PRODUCT-POLICY-1", "kind": "product_default"},
            ),
        ],
    )
    pack_id, version = store_pack(complete)
    with ctx.session_factory() as session:
        row = session.get(CompliancePolicyRulepack, pack_id)
        out["complete"] = {
            "version": row.version,
            "status": row.status,
            "is_live": CompliancePolicyRulepackStore.is_active(row),
            "rule_count": len(row.rules),
            "authorities": sorted({r["authority"] for r in row.rules}),
            "channels": sorted({r["channel"] for r in row.rules}),
        }

    # 2) a fixture whose rule has NO effective dates — valid, unbounded (version 2).
    no_dates = RulepackInput(
        jurisdiction="IN",
        title="unbounded product default",
        rules=[
            PolicyRuleInput(
                effect="deny",
                action="acquire",
                authority="product",
                evidence={"reference": "PRODUCT-DEFAULT", "kind": "product_default"},
            )
        ],
    )
    _, v2 = store_pack(no_dates)
    with ctx.session_factory() as session:
        row2 = CompliancePolicyRulepackStore(session).get_version(v2)
        out["no_effective_dates"] = {
            "version": row2.version,
            "status": row2.status,
            "effective_from": row2.rules[0]["effective_from"],
            "expires_at": row2.rules[0]["expires_at"],
        }

    rows_before_rejections = count()

    # 3) a CONTRADICTORY fixture: same scope, allow AND deny, overlapping (unbounded) window.
    contradictory = RulepackInput(
        jurisdiction="IN",
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=evidence,
            ),
            PolicyRuleInput(
                effect="deny",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=evidence,
            ),
        ],
    )
    out["contradictory_code"] = refuse(contradictory)

    # 4a) MALFORMED: non-India jurisdiction.
    foreign = RulepackInput(
        jurisdiction="US",
        rules=[
            PolicyRuleInput(effect="allow", action="acquire", authority="law", evidence=evidence)
        ],
    )
    out["non_india_code"] = refuse(foreign)

    # 4b) MALFORMED: a rule missing evidence (a rule cannot exist without citing why).
    no_evidence = RulepackInput(
        jurisdiction="IN",
        rules=[PolicyRuleInput(effect="allow", action="acquire", authority="law", evidence={})],
    )
    out["missing_evidence_code"] = refuse(no_evidence)

    rows_after_rejections = count()
    out["rows_unchanged_by_rejections"] = rows_before_rejections == rows_after_rejections

    print("\n1) complete India fixture validated -> stored as a DRAFT (never live):")
    print(
        f"   version={out['complete']['version']}  status={out['complete']['status']!r}  "
        f"live={out['complete']['is_live']}  rules={out['complete']['rule_count']}  "
        f"authorities={out['complete']['authorities']}  channels={out['complete']['channels']}"
    )
    print("\n2) fixture with NO effective dates validated (valid, unbounded rule):")
    print(
        f"   version={out['no_effective_dates']['version']}  "
        f"status={out['no_effective_dates']['status']!r}  "
        f"effective_from={out['no_effective_dates']['effective_from']}  "
        f"expires_at={out['no_effective_dates']['expires_at']}"
    )
    print("\n3) contradictory fixture (allow AND deny, same scope, overlapping window):")
    print(f"   add_version -> REFUSED  code={out['contradictory_code']!r}")
    print("\n4) malformed fixtures:")
    print(f"   non-India jurisdiction   -> REFUSED  code={out['non_india_code']!r}")
    print(f"   rule missing evidence    -> REFUSED  code={out['missing_evidence_code']!r}")
    print(
        f"\nrows before rejections = {rows_before_rejections}, after = {rows_after_rejections} "
        f"(rejections staged nothing: {out['rows_unchanged_by_rejections']})"
    )
    checks = {
        "complete_stored_as_draft": out["complete"]["status"] == PolicyRulepackStatus.DRAFT.value
        and out["complete"]["is_live"] is False,
        "no_effective_dates_valid": out["no_effective_dates"]["effective_from"] is None,
        "contradictory_refused": out["contradictory_code"] == REASON_CONTRADICTORY_RULES,
        "non_india_refused": out["non_india_code"] == REASON_INVALID_JURISDICTION,
        "missing_evidence_refused": out["missing_evidence_code"] == REASON_MISSING_RULE_EVIDENCE,
        "rejections_staged_nothing": out["rows_unchanged_by_rejections"] is True,
    }
    print(f"\nchecks: {checks}")
    out["checks"] = checks
    print(
        "\n(local synthetic only; no AI/paid action. Defining rules validates a schema and stores a "
        "reviewable DRAFT — it activates nothing: no rule becomes live just by existing. Malformed or "
        "contradictory rules are refused before anything is staged.)"
    )
    return out


@register(
    "DCA-053",
    "approve a policy version: a platform operator's complete review (references + reviewer + review "
    "date + review expiry) approves a recorded rulepack version and promotes it to active; evaluating "
    "an UNAPPROVED (draft) version is denied; a tenant administrator cannot approve (no state change); "
    "an incomplete review is refused; and modifying approved content is a NEW, unapproved version",
)
def _dca_053(ctx: CheckContext) -> dict:
    """Drive the real DCA-053 policy-approval store against a disposable DB, following the developer
    manual-test steps and showing actual rows (``compliance_policy_approvals`` is a platform registry,
    so there is no tenant HTTP surface — the inspectable artifact is the approval row, the promoted
    rulepack status and the lifecycle evaluation):

      1. record a DCA-052 India rulepack DRAFT version to review;
      2. EVALUATE the unapproved version -> DENIED (not approved); it authorizes nothing;
      3. a TENANT ADMINISTRATOR attempts the approval -> refused (not a platform operator); the
         version stays a draft and NO approval row is created;
      4. an INCOMPLETE operator review (no references) -> refused, nothing staged;
      5. a PLATFORM OPERATOR's COMPLETE review -> approved: all four essentials (references, reviewer,
         review date, review expiry) + a content fingerprint are recorded and the version is promoted
         draft -> active; evaluating it now -> IN FORCE;
      6. MODIFY the approved content -> a NEW draft version (v2), unapproved: v1 stays in force, v2
         authorizes nothing, and v1's approval fingerprint still binds v1's content (not v2's).

    Local synthetic only (AI disabled, ephemeral DB). Promoting a version authorizes no production
    behavior on its own — the policy engine that would CONSULT an in-force pack stays disabled.
    """
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from app.compliance.policy_rulepacks import (
        CompliancePolicyRulepackStore,
        PolicyRuleInput,
        RulepackInput,
    )
    from app.db.models import CompliancePolicyApproval, PolicyRulepackStatus
    from app.services.policy_approval import (
        REASON_MISSING_REFERENCES,
        REASON_NOT_APPROVED,
        REASON_NOT_PLATFORM_OPERATOR,
        PolicyApprovalAuthorizationError,
        PolicyApprovalError,
        PolicyApprovalReview,
        PolicyApprovalStore,
        PolicyReviewerPrincipal,
        content_fingerprint,
    )

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    operator = PolicyReviewerPrincipal(subject="op-001", roles=("platform_operator",))
    tenant_admin = PolicyReviewerPrincipal(subject="user-owner-A", roles=("owner", "admin"))
    evidence = {"reference": "DPDP-2023-s7", "kind": "statute", "uri": "https://example.test/law"}

    base_pack = RulepackInput(
        jurisdiction="IN",
        title="India baseline policy",
        evidence={"reference": "POLICY-REVIEW-2026-11", "kind": "legal_review"},
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=evidence,
            )
        ],
    )
    base_review = PolicyApprovalReview(
        reviewer_id="op-001",
        references={"reference": "LEGAL-REVIEW-2026-11", "kind": "legal_review"},
        reviewed_at=now,
        expires_at=now + timedelta(days=365),
    )

    def rulepack(**overrides) -> RulepackInput:
        return replace(base_pack, **overrides)

    def review(**overrides) -> PolicyApprovalReview:
        return replace(base_review, **overrides)

    out: dict = {}

    # 1) record a DCA-052 rulepack draft version
    with ctx.session_factory() as session:
        CompliancePolicyRulepackStore(session).add_version(rulepack())
        session.commit()

    # 2) evaluating the unapproved (draft) version is denied — it authorizes nothing
    with ctx.session_factory() as session:
        decision = PolicyApprovalStore(session).evaluate(1, now=now)
        out["unapproved_in_force"] = decision.in_force
        out["unapproved_reason"] = decision.reason

    # 3) a tenant administrator cannot approve — refused, nothing changes
    with ctx.session_factory() as session:
        store = PolicyApprovalStore(session)
        try:
            store.approve(1, review(reviewer_id="user-owner-A"), tenant_admin)
            out["tenant_admin_refused"] = "UNEXPECTEDLY APPROVED"
        except PolicyApprovalAuthorizationError as exc:
            out["tenant_admin_refused"] = exc.code
        session.commit()
    with ctx.session_factory() as session:
        pack = CompliancePolicyRulepackStore(session).get_version(1)
        out["status_after_tenant_attempt"] = pack.status if pack else None
        out["approvals_after_tenant_attempt"] = session.query(CompliancePolicyApproval).count()

    # 4) an incomplete operator review (no references) is refused (nothing staged)
    with ctx.session_factory() as session:
        store = PolicyApprovalStore(session)
        try:
            store.approve(1, review(references={}), operator)
            out["incomplete_refused"] = "UNEXPECTEDLY APPROVED"
        except PolicyApprovalError as exc:
            out["incomplete_refused"] = exc.code
        session.commit()
    with ctx.session_factory() as session:
        out["approvals_after_incomplete"] = session.query(CompliancePolicyApproval).count()

    # 5) a platform operator's complete review approves and promotes the version
    with ctx.session_factory() as session:
        PolicyApprovalStore(session).approve(1, review(), operator)
        session.commit()
    with ctx.session_factory() as session:
        store = PolicyApprovalStore(session)
        approval = store.approval_for(1)
        assert approval is not None
        pack = CompliancePolicyRulepackStore(session).get_version(1)
        assert pack is not None
        decision = store.evaluate(1, now=now)
        out["approved"] = {
            "jurisdiction": approval.jurisdiction,
            "policy_version": approval.policy_version,
            "reviewer_id": approval.reviewer_id,
            "reviewer_type": approval.reviewer_type,
            "references_reference": approval.review_references.get("reference"),
            "reviewed_at": approval.reviewed_at.isoformat() if approval.reviewed_at else None,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            "content_fingerprint": approval.content_fingerprint[:16] + "…",
            "in_force_now": decision.in_force,
        }
        out["status_after_approval"] = pack.status

    # 6) modify the approved content -> a NEW draft version (v2), unapproved
    with ctx.session_factory() as session:
        CompliancePolicyRulepackStore(session).add_version(
            rulepack(
                title="India baseline policy v2",
                rules=[
                    PolicyRuleInput(
                        effect="deny",  # changed effect — different content
                        action="contact",
                        channel="email",
                        authority="law",
                        conditions={"consent": "required"},
                        evidence=evidence,
                    )
                ],
            )
        )
        session.commit()
    with ctx.session_factory() as session:
        store = PolicyApprovalStore(session)
        rulepacks = CompliancePolicyRulepackStore(session)
        v1_decision = store.evaluate(1, now=now)
        v2_decision = store.evaluate(2, now=now)
        approval = store.approval_for(1)
        v1 = rulepacks.get_version(1)
        v2 = rulepacks.get_version(2)
        assert approval is not None and v1 is not None and v2 is not None
        out["after_modification"] = {
            "v1_status": v1.status,
            "v1_in_force": v1_decision.in_force,
            "v2_status": v2.status,
            "v2_in_force": v2_decision.in_force,
            "v2_reason": v2_decision.reason,
            "fingerprint_binds_v1": approval.content_fingerprint == content_fingerprint(v1.rules),
            "fingerprint_differs_v2": approval.content_fingerprint
            != content_fingerprint(v2.rules),
        }

    print("\n1) recorded a DCA-052 India rulepack DRAFT (version 1).")
    print("\n2) evaluate the UNAPPROVED version:")
    print(
        f"   in_force={out['unapproved_in_force']}  reason={out['unapproved_reason']!r} "
        "(a draft authorizes nothing)"
    )
    print("\n3) tenant administrator attempts the approval:")
    print(
        f"   owner/admin@A -> REFUSED ({out['tenant_admin_refused']})  "
        f"status still '{out['status_after_tenant_attempt']}'  "
        f"approvals={out['approvals_after_tenant_attempt']}"
    )
    print("\n4) incomplete operator review (no references):")
    print(
        f"   -> refused ({out['incomplete_refused']})  "
        f"approvals after refusal: {out['approvals_after_incomplete']}"
    )
    print("\n5) platform operator's COMPLETE review -> approved (actual approval row):")
    for k, v in out["approved"].items():
        print(f"     {k:22} = {v}")
    print(
        f"   endorsed version promoted: status now '{out['status_after_approval']}' (was 'draft')"
    )
    print("\n6) modify approved content -> a NEW, unapproved version:")
    for k, v in out["after_modification"].items():
        print(f"     {k:22} = {v}")

    checks = {
        "unapproved_denied": out["unapproved_reason"] == REASON_NOT_APPROVED,
        "tenant_admin_refused": out["tenant_admin_refused"] == REASON_NOT_PLATFORM_OPERATOR,
        "no_state_change_on_tenant_attempt": (
            out["status_after_tenant_attempt"] == PolicyRulepackStatus.DRAFT.value
            and out["approvals_after_tenant_attempt"] == 0
        ),
        "incomplete_refused": out["incomplete_refused"] == REASON_MISSING_REFERENCES,
        "incomplete_staged_nothing": out["approvals_after_incomplete"] == 0,
        "operator_approved_and_promoted": (
            out["approved"]["reviewer_type"] == "platform_operator"
            and out["status_after_approval"] == PolicyRulepackStatus.ACTIVE.value
            and out["approved"]["in_force_now"] is True
        ),
        "modification_is_new_unapproved_version": (
            out["after_modification"]["v1_in_force"] is True
            and out["after_modification"]["v2_in_force"] is False
            and out["after_modification"]["v2_reason"] == REASON_NOT_APPROVED
            and out["after_modification"]["fingerprint_binds_v1"] is True
            and out["after_modification"]["fingerprint_differs_v2"] is True
        ),
    }
    print(f"\nchecks: {checks}")
    out["checks"] = checks
    print(
        "\n(local synthetic only; no AI/paid action. Approval is a PLATFORM-operator action — a tenant "
        "administrator cannot perform it; the approval records references/reviewer/review-date/expiry "
        "and a content fingerprint, so changing approved content requires a NEW version. A draft or "
        "expired version authorizes nothing, and promoting a version does NOT enable the policy engine "
        f"(a disabled future capability). Table used: {CompliancePolicyApproval.__tablename__} — a "
        "platform control-plane registry, not tenant-scoped.)"
    )
    return out


def _confirm_icp(client, business_id, version, actor_id, auth):
    return client.post(
        f"/v1/tenants/{business_id}/icp/confirm",
        json={"version": version, "actorId": actor_id},
        headers=auth,
    )


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
