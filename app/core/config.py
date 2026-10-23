"""DCA settings — fail-fast, env-driven, everything risky OFF by default.

Mirrors the effortlesspromo-ai conventions (pydantic-settings, ``validate_required`` at startup).
Env prefix is ``EFFORTLESSPROMO_DCA_`` so the two Python services never collide.

Contract notes (see docs/DCA-001-integration-contracts.md):
- The AI service authenticates callers with a shared secret only and performs NO tenant
  authorization (B3) — this service is responsible for only ever asserting business IDs it has
  itself authorized, and for never exposing an endpoint that lets one tenant act as another.
- AI-side idempotency is in-memory (B4) — this service keeps its own durable AiInvocation rows.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EFFORTLESSPROMO_DCA_", env_file=".env")

    # --- required (validate_required fails the boot when missing) ---
    environment: str | None = None          # development | testing | production
    database_url: str | None = None         # the DCA-owned database; never the platform DB (ADR-0005)

    # --- inbound service auth (fail-closed, same pattern as effortlesspromo-ai) ---
    service_auth_token: str | None = None

    # --- outbound: existing effortlesspromo-ai gateway (all model execution goes there) ---
    ai_base_url: str | None = None          # empty => AI-dependent features disabled
    ai_service_token: str | None = None     # must equal the AI service's service_auth_token
    ai_timeout_seconds: float = 100.0       # matches the .NET caller's effective default

    # --- worker/scheduler (24/7 loop) — OFF by default ---
    worker_enabled: bool = False
    worker_interval_seconds: int = 3600

    # --- sources — nothing fetches anything until explicitly enabled ---
    synthetic_sources_enabled: bool = False  # local synthetic fixtures only; no live source exists yet

    # --- scope guards ---
    allowed_countries: list[str] = ["IN"]    # India-only build (DCA story boundary)
    candidate_ttl_days: int = 30

    # --- schema management (no Alembic yet; see README) ---
    auto_create_schema: bool = True          # dev/test convenience; set false once migrations exist


REQUIRED_KEYS = ("environment", "database_url")


def validate_required(settings: Settings) -> None:
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
