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
