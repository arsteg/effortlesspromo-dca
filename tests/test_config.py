import pytest

from app.core.config import Settings, validate_required


def test_missing_required_fails_fast():
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_ENVIRONMENT"):
        validate_required(Settings(_env_file=None, database_url="sqlite://"))
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_DATABASE_URL"):
        validate_required(Settings(_env_file=None, environment="testing"))


def test_india_only_boundary_enforced():
    bad = Settings(
        _env_file=None, environment="testing", database_url="sqlite://", allowed_countries=["IN", "US"]
    )
    with pytest.raises(RuntimeError, match="India-only"):
        validate_required(bad)


def test_risky_features_default_off():
    s = Settings(_env_file=None, environment="testing", database_url="sqlite://")
    assert s.worker_enabled is False
    assert s.synthetic_sources_enabled is False
    assert s.service_auth_token is None  # fail-closed: no token => 401 everywhere privileged
