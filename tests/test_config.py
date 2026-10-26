import pytest

from app.core.config import DeploymentMode, SecretSource, Settings, validate_required


def _base(**overrides) -> Settings:
    base = {"_env_file": None, "environment": "testing", "database_url": "sqlite://"}
    base.update(overrides)
    return Settings(**base)


def test_missing_required_fails_fast():
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_ENVIRONMENT"):
        validate_required(Settings(_env_file=None, database_url="sqlite://"))
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_DATABASE_URL"):
        validate_required(Settings(_env_file=None, environment="testing"))


def test_india_only_boundary_enforced():
    bad = Settings(
        _env_file=None,
        environment="testing",
        database_url="sqlite://",
        allowed_countries=["IN", "US"],
    )
    with pytest.raises(RuntimeError, match="India-only"):
        validate_required(bad)


def test_risky_features_default_off():
    s = Settings(_env_file=None, environment="testing", database_url="sqlite://")
    assert s.worker_enabled is False
    assert s.synthetic_sources_enabled is False
    assert s.service_auth_token is None  # fail-closed: no token => 401 everywhere privileged
    assert s.deployment_mode is DeploymentMode.FAKE  # safe default; prod must opt into LIVE
    assert s.secret_source is SecretSource.ENV
    assert s.ai_enabled is False  # integrations disabled until explicitly configured


# --- DCA-004: no model-provider keys live here (they belong only to effortlesspromo-ai) ---


def test_no_provider_api_keys_in_settings():
    names = set(Settings.model_fields)
    assert not any("api_key" in n or "openai" in n or "anthropic" in n for n in names), names


# --- DCA-004: production rejects fake mode ---


def test_production_rejects_fake_deployment_mode():
    bad = _base(
        environment="production",
        deployment_mode=DeploymentMode.FAKE,
        service_auth_token="real",
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    with pytest.raises(RuntimeError, match="production rejects fake deployment mode"):
        validate_required(bad)


def test_production_live_mode_passes_when_hardened():
    ok = _base(
        environment="production",
        deployment_mode=DeploymentMode.LIVE,
        service_auth_token="real",
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    validate_required(ok)  # no raise


def test_production_rejects_failopen_and_demo_defaults():
    # names only, never the value
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_SERVICE_AUTH_TOKEN"):
        validate_required(
            _base(
                environment="production",
                deployment_mode=DeploymentMode.LIVE,
                synthetic_sources_enabled=False,
                auto_create_schema=False,
            )
        )
    with pytest.raises(RuntimeError, match="SYNTHETIC_SOURCES_ENABLED"):
        validate_required(
            _base(
                environment="production",
                deployment_mode=DeploymentMode.LIVE,
                service_auth_token="real",
                synthetic_sources_enabled=True,
                auto_create_schema=False,
            )
        )
    with pytest.raises(RuntimeError, match="AUTO_CREATE_SCHEMA"):
        validate_required(
            _base(
                environment="production",
                deployment_mode=DeploymentMode.LIVE,
                service_auth_token="real",
                synthetic_sources_enabled=False,
                auto_create_schema=True,
            )
        )


# --- DCA-004: AI integration references ---


def test_disabled_ai_integration_needs_no_token():
    # no base url => AI disabled => no credential required, valid config
    s = _base()
    assert s.ai_enabled is False
    validate_required(s)  # no raise


def test_enabled_ai_without_service_token_is_rejected():
    bad = _base(ai_base_url="http://ai.internal:8000")
    assert bad.ai_enabled is True
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_AI_SERVICE_TOKEN"):
        validate_required(bad)


def test_enabled_ai_with_service_token_passes():
    validate_required(_base(ai_base_url="http://ai.internal:8000", ai_service_token="shared"))


# --- DCA-004: secret references ---


def test_non_env_secret_source_requires_references():
    bad = _base(secret_source=SecretSource.AWS_SECRETS_MANAGER)
    with pytest.raises(RuntimeError, match="DATABASE_URL_SECRET_REF"):
        validate_required(bad)


def test_non_env_secret_source_passes_with_references():
    ok = _base(
        secret_source=SecretSource.VAULT,
        database_url_secret_ref="secret/dca/db",
        service_auth_token_secret_ref="secret/dca/svc",
    )
    validate_required(ok)  # AI disabled => no ai ref required


# --- DCA-032: platform HTTP transport configuration ---


def test_platform_transport_off_by_default():
    s = _base()
    assert s.platform_base_url is None
    assert s.platform_transport_configured is False
    validate_required(s)  # unconfigured => valid


def test_platform_base_url_without_credential_is_rejected():
    bad = _base(platform_base_url="https://platform.internal/svc")
    assert bad.platform_transport_configured is False
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_PLATFORM_SERVICE_CREDENTIAL"):
        validate_required(bad)


def test_platform_transport_configured_passes():
    ok = _base(
        platform_base_url="https://platform.internal/svc",
        platform_service_credential="scoped-cred",
    )
    assert ok.platform_transport_configured is True
    validate_required(ok)  # no raise


def test_platform_timeouts_must_be_positive():
    with pytest.raises(RuntimeError, match="PLATFORM_.*TIMEOUT_SECONDS"):
        validate_required(
            _base(
                platform_base_url="https://platform.internal/svc",
                platform_service_credential="scoped-cred",
                platform_connect_timeout_seconds=0,
            )
        )


def test_platform_transport_non_env_secret_source_requires_reference():
    bad = _base(
        secret_source=SecretSource.VAULT,
        database_url_secret_ref="secret/dca/db",
        service_auth_token_secret_ref="secret/dca/svc",
        platform_base_url="https://platform.internal/svc",
        platform_service_credential="scoped-cred",
    )
    with pytest.raises(RuntimeError, match="PLATFORM_SERVICE_CREDENTIAL_SECRET_REF"):
        validate_required(bad)
