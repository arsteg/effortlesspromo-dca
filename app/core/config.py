"""DCA settings — fail-fast, env-driven, everything risky OFF by default.

Mirrors the effortlesspromo-ai conventions (pydantic-settings, ``validate_required`` at startup).
Env prefix is ``EFFORTLESSPROMO_DCA_`` so the two Python services never collide.

Contract notes (see docs/DCA-001-integration-contracts.md):
- The AI service authenticates callers with a shared secret only and performs NO tenant
  authorization (B3) — this service is responsible for only ever asserting business IDs it has
  itself authorized, and for never exposing an endpoint that lets one tenant act as another.
- AI-side idempotency is in-memory (B4) — this service keeps its own durable AiInvocation rows.

Secrets are NEVER ordinary configuration (SEC-007 / BR-125, same stance as effortlesspromo-ai):
the *values* arrive out-of-band (env var, injected file, or a secret manager named by the
``*_secret_ref`` fields below). Config only ever carries a *reference* to where a secret lives,
never the secret itself in code. **No model-provider API key exists here at all** — all model
execution is delegated to effortlesspromo-ai, which owns the only provider keys (ADR-0004 / DCA
boundary).
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentMode(str, Enum):
    """How this instance is wired to the outside world.

    ``FAKE`` is the safe local/demo profile: synthetic fixtures, stubbable AI, throwaway DB — nothing
    real can leave the box. ``LIVE`` is the real profile: real DB, real inbound auth, and (if AI is
    configured) a real AI service token. Production MUST be ``LIVE`` — fake mode is rejected at boot.
    """

    FAKE = "fake"
    LIVE = "live"


class SecretSource(str, Enum):
    """Where secret *values* are sourced from. The config only ever holds a reference, never a value.

    ``ENV`` — secrets injected as environment variables / local .env (dev default).
    ``AWS_SECRETS_MANAGER`` / ``VAULT`` — secrets resolved out-of-band from the named ``*_secret_ref``.
    No secret manager client is wired yet; selecting a non-env source requires the matching refs so a
    misconfiguration fails at boot rather than silently falling back to env (a later story adds the
    resolver — see README deferrals).
    """

    ENV = "env"
    AWS_SECRETS_MANAGER = "aws_secrets_manager"
    VAULT = "vault"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EFFORTLESSPROMO_DCA_", env_file=".env")

    # --- required (validate_required fails the boot when missing) ---
    environment: str | None = None  # development | testing | production
    deployment_mode: DeploymentMode = (
        DeploymentMode.FAKE
    )  # production rejects FAKE (see validate_required)

    # --- the DCA-owned datastore (its own database + schema; never the platform DB, ADR-0005) ---
    database_url: str | None = None  # required; the DCA-owned database
    db_schema: str = "dca"  # logical schema namespace within the DCA database

    # --- secret handling (values are out-of-band; config carries references only, never values) ---
    secret_source: SecretSource = SecretSource.ENV
    database_url_secret_ref: str | None = None  # e.g. an AWS Secrets Manager ARN / Vault path
    service_auth_token_secret_ref: str | None = None
    ai_service_token_secret_ref: str | None = None

    # --- inbound service auth (fail-closed, same pattern as effortlesspromo-ai) ---
    # Coarse shared secret authenticating ANY calling service (legacy / existing /v1/tenants surface).
    service_auth_token: str | None = None

    # --- inbound SCOPED service credentials (DCA-020): signed, per-operation/per-business ---
    # A distinct credential type for platform integration requests: a JWT bound to issuer/audience/
    # expiry plus explicit allowed operations and businesses. Deliberately NOT the end-user
    # (public-form) token — a different audience/signing secret, so one can never stand in for the
    # other. Only HS256; disabled (fail-closed) until issuer/audience/signing-secret are all set.
    service_token_issuer: str | None = None
    service_token_audience: str | None = None
    service_token_signing_secret: str | None = None
    service_token_signing_secret_ref: str | None = None
    service_token_algorithm: str = "HS256"
    service_token_leeway_seconds: int = 60

    # --- inbound END-USER authentication (DCA-015): validate a caller's bearer token ---
    # The service-auth gate above authenticates the calling SERVICE; this validates the end user whose
    # bearer token the caller forwards. Only HS256 (symmetric) is implemented here; asymmetric
    # (RS256/JWKS) and token introspection are a later story (the live platform adapter, B1) and stay
    # disabled until then. When none of issuer/audience/signing-secret are set the contract is
    # disabled: production fails closed (every call 401), dev/test falls back to the obviously-fake
    # verifier (never in production — mirrors the identity fake and the .NET dev raw-id fallback).
    auth_token_issuer: str | None = None  # expected `iss` claim
    auth_token_audience: str | None = None  # expected `aud` claim
    auth_token_signing_secret: str | None = None  # HS256 shared secret (value injected out-of-band)
    auth_token_signing_secret_ref: str | None = None
    auth_token_algorithm: str = "HS256"
    auth_token_leeway_seconds: int = 60  # clock-skew tolerance for exp/nbf

    # --- inbound webhook transport auth (DCA-030): HMAC signature + timestamp tolerance + rotation ---
    # Authenticates the raw inbound-event bytes BEFORE the DCA-029 envelope is parsed (it does not
    # authorize any tenant — B3). Fail-closed and OFF by default: no key configured => every webhook is
    # refused, and no live inbound route is wired yet (this capability stays disabled until a later
    # story mounts the transport). Secrets are VALUES injected out-of-band; config only names which key
    # id is current/previous. A *previous* key is kept populated through a rotation OVERLAP so a message
    # signed by either the old or the new key verifies; it is removed once every producer has moved.
    webhook_verification_enabled: bool = False
    webhook_signing_secret: str | None = None  # current key secret (value injected out-of-band)
    webhook_signing_secret_ref: str | None = None
    webhook_current_key_id: str = "current"
    webhook_previous_signing_secret: str | None = None  # kept only during a rotation overlap
    webhook_previous_signing_secret_ref: str | None = None
    webhook_previous_key_id: str = "previous"
    webhook_timestamp_tolerance_seconds: int = 300  # ±5 min replay/skew window

    # --- outbound: existing effortlesspromo-ai gateway (all model execution goes there) ---
    ai_base_url: str | None = None  # empty => AI-dependent features disabled
    ai_service_token: str | None = None  # must equal the AI service's service_auth_token
    ai_timeout_seconds: float = 100.0  # matches the .NET caller's effective default

    # --- outbound: the platform (.NET API) HTTP transport (DCA-032) — OFF by default ---
    # The hardened transport MECHANISM for calling the platform API: single allowlisted origin,
    # mandatory scoped credential, explicit timeouts, correlation ids, safe error mapping. It is only
    # the transport — business adapters (suppression/CRM/entitlement/delivery) are separate and stay
    # disabled until DCA-001 blocker B1 (a scoped service contract into the .NET API) ships. Disabled
    # until BOTH a base URL and a scoped credential are set (fail-closed); an empty base URL => off.
    # The credential VALUE arrives out-of-band (env/secret manager); config carries only a reference
    # (``*_secret_ref``), never the value itself — same stance as every other DCA secret.
    platform_base_url: str | None = None  # empty => platform transport disabled
    platform_service_credential: str | None = None  # scoped credential VALUE (injected out-of-band)
    platform_service_credential_secret_ref: str | None = None
    platform_credential_header: str = "Authorization"
    platform_credential_scheme: str = "Bearer"  # "" => send the bare credential with no scheme word
    platform_connect_timeout_seconds: float = 5.0
    platform_timeout_seconds: float = 30.0

    # --- DCA feature flag (DCA-021) — OFF by default ---
    # The platform-level master switch for the Data Concierge feature. Off => no business can start
    # work regardless of its own enablement. Enabling it is the *platform* enablement; each business
    # is then narrowed in by its own TenantAgent.enabled flag (business narrowing).
    feature_enabled: bool = False

    # --- entitlement enforcement (DCA-023) — OFF by default ---
    # When on, a chargeable operation (a discovery cycle) is gated on a fresh, active subscription
    # entitlement (DCA-022). A cached projection authorizes only while it is younger than
    # entitlement_max_age_seconds, so a prior-active subscription cannot keep authorizing paid work
    # indefinitely while the platform entitlement host is unreachable. Off by default (the live
    # entitlement adapter is unbuilt; the contract stays disabled until it ships).
    entitlement_enforcement_enabled: bool = False
    entitlement_max_age_seconds: int = 86400  # 24h freshness bound on a cached projection

    # --- worker/scheduler (24/7 loop) — OFF by default ---
    worker_enabled: bool = False
    worker_interval_seconds: int = 3600

    # --- sources — nothing fetches anything until explicitly enabled ---
    synthetic_sources_enabled: bool = (
        False  # local synthetic fixtures only; no live source exists yet
    )

    # --- scope guards ---
    allowed_countries: list[str] = ["IN"]  # India-only build (DCA story boundary)
    candidate_ttl_days: int = 30

    # --- schema management (no Alembic yet; see README) ---
    auto_create_schema: bool = True  # dev/test convenience; set false once migrations exist

    # --- derived helpers (not config; convenience for the rest of the app) ---
    @property
    def is_production(self) -> bool:
        return (self.environment or "").strip().lower() == "production"

    @property
    def ai_enabled(self) -> bool:
        """AI integration stays disabled until a base URL is explicitly configured."""
        return bool((self.ai_base_url or "").strip())

    @property
    def platform_transport_configured(self) -> bool:
        """The platform HTTP transport is active only when BOTH a base URL and a scoped credential
        are set. A base URL without a credential is rejected at boot (``validate_required``); unset
        => disabled (fail-closed — no transport, so every adapter built on it stays off, B1)."""
        return bool(
            (self.platform_base_url or "").strip()
            and (self.platform_service_credential or "").strip()
        )

    @property
    def end_user_auth_configured(self) -> bool:
        """End-user token validation is active only when issuer, audience AND signing secret are all
        set. A partial configuration is rejected at boot (``validate_required``) so auth is never
        half-wired; when nothing is set the contract is disabled (fail-closed in production)."""
        return all(
            (
                (self.auth_token_issuer or "").strip(),
                (self.auth_token_audience or "").strip(),
                (self.auth_token_signing_secret or "").strip(),
            )
        )

    @property
    def service_credentials_configured(self) -> bool:
        """Scoped service-credential validation is active only when its issuer, audience AND signing
        secret are all set. Partial configuration is rejected at boot; unset => disabled (fail-closed
        in production)."""
        return all(
            (
                (self.service_token_issuer or "").strip(),
                (self.service_token_audience or "").strip(),
                (self.service_token_signing_secret or "").strip(),
            )
        )

    @property
    def webhook_verification_configured(self) -> bool:
        """Inbound webhook verification is active only when it is enabled AND a current signing secret
        is present. Enabled-but-secretless is rejected at boot; unset => disabled (fail-closed in
        production — an empty keyring trusts nothing)."""
        return self.webhook_verification_enabled and bool(
            (self.webhook_signing_secret or "").strip()
        )


REQUIRED_KEYS = ("environment", "database_url")


def validate_required(settings: Settings) -> None:
    """Fail fast on unsafe configuration, naming the offending key only (never leaking any value)."""
    missing = [key for key in REQUIRED_KEYS if not getattr(settings, key)]
    if missing:
        raise RuntimeError(
            "missing required configuration: "
            + ", ".join(f"EFFORTLESSPROMO_DCA_{key.upper()}" for key in missing)
        )

    for country in settings.allowed_countries:
        if country != "IN":
            raise RuntimeError(
                f"country '{country}' is not supported: this build is India-only (DCA boundary)"
            )

    # AI integration, when enabled, must carry its service token — else every gateway call 401s
    # fail-closed. This holds in every environment (a half-wired AI client is never useful).
    if settings.ai_enabled and not (settings.ai_service_token or "").strip():
        raise RuntimeError(
            "AI integration is enabled (EFFORTLESSPROMO_DCA_AI_BASE_URL set) but "
            "EFFORTLESSPROMO_DCA_AI_SERVICE_TOKEN is missing"
        )

    # DCA-032: the platform HTTP transport, when its base URL is set, MUST carry a scoped credential —
    # an allowlisted endpoint with no credential would call the platform unauthenticated. Fail-closed
    # in every environment (a half-wired transport is never useful). Timeouts must be positive so a
    # call is never unbounded.
    if (settings.platform_base_url or "").strip() and not (
        settings.platform_service_credential or ""
    ).strip():
        raise RuntimeError(
            "platform transport base URL is set (EFFORTLESSPROMO_DCA_PLATFORM_BASE_URL) but "
            "EFFORTLESSPROMO_DCA_PLATFORM_SERVICE_CREDENTIAL is missing"
        )
    if settings.platform_connect_timeout_seconds <= 0 or settings.platform_timeout_seconds <= 0:
        raise RuntimeError(
            "EFFORTLESSPROMO_DCA_PLATFORM_CONNECT_TIMEOUT_SECONDS and "
            "EFFORTLESSPROMO_DCA_PLATFORM_TIMEOUT_SECONDS must be positive"
        )

    # DCA-015: end-user token authentication. Only HS256 (symmetric) is implemented; asymmetric
    # (RS256/JWKS) and token introspection are a later story and stay disabled — a missing contract is
    # never silently served, so an unsupported algorithm fails boot rather than mis-validating.
    if settings.auth_token_algorithm.strip().upper() != "HS256":
        raise RuntimeError(
            f"EFFORTLESSPROMO_DCA_AUTH_TOKEN_ALGORITHM '{settings.auth_token_algorithm}' is not "
            "supported: this build validates HS256 only (asymmetric/JWKS/introspection is a later "
            "story and stays disabled)"
        )

    # End-user auth must be fully wired or not at all: a half-set trio would silently disable
    # validation (fake in dev, closed in prod) instead of doing what the operator intended.
    auth_parts = {
        "EFFORTLESSPROMO_DCA_AUTH_TOKEN_ISSUER": (settings.auth_token_issuer or "").strip(),
        "EFFORTLESSPROMO_DCA_AUTH_TOKEN_AUDIENCE": (settings.auth_token_audience or "").strip(),
        "EFFORTLESSPROMO_DCA_AUTH_TOKEN_SIGNING_SECRET": (
            settings.auth_token_signing_secret or ""
        ).strip(),
    }
    set_parts = [key for key, value in auth_parts.items() if value]
    if set_parts and len(set_parts) != len(auth_parts):
        unset = [key for key, value in auth_parts.items() if not value]
        raise RuntimeError(
            "end-user authentication is partially configured; set all of issuer/audience/signing "
            "secret or none. missing: " + ", ".join(unset)
        )

    # DCA-020: scoped service credentials. HS256 only, same stance as end-user auth.
    if settings.service_token_algorithm.strip().upper() != "HS256":
        raise RuntimeError(
            f"EFFORTLESSPROMO_DCA_SERVICE_TOKEN_ALGORITHM '{settings.service_token_algorithm}' is "
            "not supported: this build validates HS256 only"
        )
    svc_parts = {
        "EFFORTLESSPROMO_DCA_SERVICE_TOKEN_ISSUER": (settings.service_token_issuer or "").strip(),
        "EFFORTLESSPROMO_DCA_SERVICE_TOKEN_AUDIENCE": (
            settings.service_token_audience or ""
        ).strip(),
        "EFFORTLESSPROMO_DCA_SERVICE_TOKEN_SIGNING_SECRET": (
            settings.service_token_signing_secret or ""
        ).strip(),
    }
    svc_set = [key for key, value in svc_parts.items() if value]
    if svc_set and len(svc_set) != len(svc_parts):
        unset = [key for key, value in svc_parts.items() if not value]
        raise RuntimeError(
            "service credentials are partially configured; set all of issuer/audience/signing "
            "secret or none. missing: " + ", ".join(unset)
        )
    # Do NOT reuse public-form (end-user) tokens as service credentials: when both are configured
    # their audiences MUST differ, so a token minted for one is never accepted by the other.
    if (
        settings.service_credentials_configured
        and settings.end_user_auth_configured
        and (settings.service_token_audience or "").strip()
        == (settings.auth_token_audience or "").strip()
    ):
        raise RuntimeError(
            "service-credential and end-user token audiences must differ "
            "(EFFORTLESSPROMO_DCA_SERVICE_TOKEN_AUDIENCE vs EFFORTLESSPROMO_DCA_AUTH_TOKEN_AUDIENCE): "
            "service credentials must not reuse public-form tokens"
        )

    # DCA-030: inbound webhook verification, when enabled, must carry a current signing secret — an
    # enabled-but-secretless gate would silently trust nothing (fail-closed) instead of doing what the
    # operator intended. The tolerance window must be non-negative. A previous key without the feature
    # enabled is harmless (ignored), but a configured previous secret with no current one is nonsense.
    if (
        settings.webhook_verification_enabled
        and not (settings.webhook_signing_secret or "").strip()
    ):
        raise RuntimeError(
            "webhook verification is enabled (EFFORTLESSPROMO_DCA_WEBHOOK_VERIFICATION_ENABLED) but "
            "EFFORTLESSPROMO_DCA_WEBHOOK_SIGNING_SECRET is missing"
        )
    if settings.webhook_timestamp_tolerance_seconds < 0:
        raise RuntimeError(
            "EFFORTLESSPROMO_DCA_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS must be non-negative"
        )
    if (settings.webhook_previous_signing_secret or "").strip() and not (
        settings.webhook_signing_secret or ""
    ).strip():
        raise RuntimeError(
            "a previous webhook signing secret is set without a current one "
            "(EFFORTLESSPROMO_DCA_WEBHOOK_SIGNING_SECRET): a rotation overlap needs the current key"
        )

    # A non-env secret source must name where each configured secret lives, or boot fails rather than
    # silently reading the value from the environment.
    if settings.secret_source is not SecretSource.ENV:
        missing_refs: list[str] = []
        if not (settings.database_url_secret_ref or "").strip():
            missing_refs.append("EFFORTLESSPROMO_DCA_DATABASE_URL_SECRET_REF")
        if not (settings.service_auth_token_secret_ref or "").strip():
            missing_refs.append("EFFORTLESSPROMO_DCA_SERVICE_AUTH_TOKEN_SECRET_REF")
        if settings.ai_enabled and not (settings.ai_service_token_secret_ref or "").strip():
            missing_refs.append("EFFORTLESSPROMO_DCA_AI_SERVICE_TOKEN_SECRET_REF")
        if (
            settings.platform_transport_configured
            and not (settings.platform_service_credential_secret_ref or "").strip()
        ):
            missing_refs.append("EFFORTLESSPROMO_DCA_PLATFORM_SERVICE_CREDENTIAL_SECRET_REF")
        if (
            settings.end_user_auth_configured
            and not (settings.auth_token_signing_secret_ref or "").strip()
        ):
            missing_refs.append("EFFORTLESSPROMO_DCA_AUTH_TOKEN_SIGNING_SECRET_REF")
        if (
            settings.service_credentials_configured
            and not (settings.service_token_signing_secret_ref or "").strip()
        ):
            missing_refs.append("EFFORTLESSPROMO_DCA_SERVICE_TOKEN_SIGNING_SECRET_REF")
        if (
            settings.webhook_verification_configured
            and not (settings.webhook_signing_secret_ref or "").strip()
        ):
            missing_refs.append("EFFORTLESSPROMO_DCA_WEBHOOK_SIGNING_SECRET_REF")
        if (
            settings.webhook_verification_configured
            and (settings.webhook_previous_signing_secret or "").strip()
            and not (settings.webhook_previous_signing_secret_ref or "").strip()
        ):
            missing_refs.append("EFFORTLESSPROMO_DCA_WEBHOOK_PREVIOUS_SIGNING_SECRET_REF")
        if missing_refs:
            raise RuntimeError(
                f"secret source '{settings.secret_source.value}' requires secret references: "
                + ", ".join(missing_refs)
            )

    if settings.is_production:
        _validate_production(settings)


def _validate_production(settings: Settings) -> None:
    """Production refuses fake mode and every demo/fail-open default — names only, no values."""
    if settings.deployment_mode is DeploymentMode.FAKE:
        raise RuntimeError(
            "production rejects fake deployment mode: set EFFORTLESSPROMO_DCA_DEPLOYMENT_MODE=live"
        )
    problems: list[str] = []
    if not (settings.service_auth_token or "").strip():
        # fail-open inbound auth is never acceptable in production
        problems.append("EFFORTLESSPROMO_DCA_SERVICE_AUTH_TOKEN")
    if settings.synthetic_sources_enabled:
        problems.append("EFFORTLESSPROMO_DCA_SYNTHETIC_SOURCES_ENABLED must be false in production")
    if settings.auto_create_schema:
        problems.append("EFFORTLESSPROMO_DCA_AUTO_CREATE_SCHEMA must be false in production")
    if problems:
        raise RuntimeError("invalid production configuration: " + ", ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
